"use client";

/**
 * CultureCard — 회사 "컬처" 정보 카드. 고정 스키마가 아니라 노션처럼
 * 항목(라벨+값)을 자유롭게 추가/수정/삭제할 수 있는 동적 구조다.
 * 구 고정 필드(office_address 등)는 최초 로드시 동적 항목으로 마이그레이션한다.
 * content.free_page_id(자유 섹션)는 그대로 보존한다.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Card, Skeleton, ToolbarButton } from "@/components/ui";
import { TextInput, TextArea, labelStyle } from "@/components/dashboard/kit";
import { PageStack } from "@/components/dashboard/pageEditor";

interface CultureField {
  id: string;
  label: string;
  value: string;
  multiline?: boolean;
}

// 구 고정 키 → 항목 라벨/형식. 기존 저장 데이터를 동적 항목으로 옮길 때 사용.
const LEGACY_FIELDS: { key: string; label: string; multiline?: boolean }[] = [
  { key: "office_address", label: "주소", multiline: false },
  { key: "postal_code", label: "우편번호" },
  { key: "wifi_ssid", label: "WiFi 이름(SSID)" },
  { key: "wifi_password", label: "WiFi 비밀번호" },
  { key: "work_hours", label: "근무 시간" },
  { key: "work_note", label: "근무 안내", multiline: true },
];
const LEGACY_KEYS = LEGACY_FIELDS.map((f) => f.key);

function uid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `f-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// content → 동적 항목 배열. fields가 이미 있으면 그대로, 없으면 구 고정 키에서 생성.
function fieldsFromContent(content: Record<string, unknown>): CultureField[] {
  const raw = content.fields;
  if (Array.isArray(raw)) {
    return raw
      .filter((f): f is Record<string, unknown> => !!f && typeof f === "object")
      .map((f) => ({
        id: typeof f.id === "string" ? f.id : uid(),
        label: typeof f.label === "string" ? f.label : "",
        value: typeof f.value === "string" ? f.value : "",
        multiline: Boolean(f.multiline),
      }));
  }
  return LEGACY_FIELDS.map((f) => ({
    id: uid(),
    label: f.label,
    value: typeof content[f.key] === "string" ? (content[f.key] as string) : "",
    multiline: f.multiline,
  }));
}

export function CultureCard() {
  const [fields, setFields] = useState<CultureField[]>([]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<CultureField[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 자유 본문(노션식 섹션/토글/하위페이지)을 담는 dashboard_page 싱글톤 id.
  const [content, setContent] = useState<Record<string, unknown>>({});
  const [freePageId, setFreePageId] = useState<string | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const ensuring = useRef(false);

  // 조회는 읽기만 한다(뷰=쓰기 금지). 자유 섹션 페이지는 여기서 만들지 않는다.
  useEffect(() => {
    let alive = true;
    api
      .getCulture()
      .then((c) => {
        if (!alive) return;
        const cont = (c.content as Record<string, unknown>) ?? {};
        setContent(cont);
        setFields(fieldsFromContent(cont));
        // 자유 섹션 페이지는 조회 시 만들지 않는다(뷰=쓰기 금지). content에 있으면만 노출.
        setFreePageId((cont.free_page_id as string) || null);
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

  // 자유 섹션 페이지는 사용자가 명시적으로 누를 때만 만든다(조회마다 자동 생성 →
  // 동시 조회자 중복 페이지 race를 막는다). 생성 후 id를 컬처 content에 보존 저장.
  const [addingSection, setAddingSection] = useState(false);
  async function addFreeSection() {
    if (ensuring.current || freePageId) return;
    ensuring.current = true;
    setAddingSection(true);
    setError(null);
    try {
      const p = await api.createDashboardPage({ title: "회사 컬처", kind: "page" });
      const nextContent = { ...content, free_page_id: p.page_id };
      await api.saveCulture(nextContent);
      setContent(nextContent);
      setFreePageId(p.page_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "자유 섹션 추가 실패");
      ensuring.current = false; // 실패 시 재시도 허용
    } finally {
      setAddingSection(false);
    }
  }

  function startEdit() {
    setDraft(fields.map((f) => ({ ...f })));
    setEditing(true);
  }

  function updateField(id: string, patch: Partial<CultureField>) {
    setDraft((d) => d.map((f) => (f.id === id ? { ...f, ...patch } : f)));
  }
  function addField() {
    setDraft((d) => [...d, { id: uid(), label: "", value: "", multiline: false }]);
  }
  function removeField(id: string) {
    setDraft((d) => d.filter((f) => f.id !== id));
  }
  // 드래그한 항목(dragId)을 target 항목 위치로 이동해 순서를 바꾼다.
  function reorder(targetId: string) {
    setDraft((d) => {
      if (!dragId || dragId === targetId) return d;
      const from = d.findIndex((f) => f.id === dragId);
      const to = d.findIndex((f) => f.id === targetId);
      if (from < 0 || to < 0) return d;
      const next = [...d];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
  }

  async function save() {
    setBusy(true);
    setError(null);
    // 라벨/값이 모두 빈 항목은 버리고 저장. 구 고정 키는 제거하고 fields로 통일.
    const cleaned = draft
      .map((f) => ({ ...f, label: f.label.trim() }))
      .filter((f) => f.label !== "" || f.value.trim() !== "");
    const base: Record<string, unknown> = { ...content };
    LEGACY_KEYS.forEach((k) => delete base[k]);
    try {
      const res = await api.saveCulture({ ...base, fields: cleaned });
      const cont = (res.content as Record<string, unknown>) ?? {};
      setContent(cont);
      setFields(fieldsFromContent(cont));
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  const visible = fields.filter((f) => f.value.trim() !== "" || f.label.trim() !== "");

  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2
          className="text-[var(--text-body)] font-bold"
          style={{ color: "var(--color-text-strong)" }}
        >
          컬처
        </h2>
        {!editing ? (
          <button
            type="button"
            onClick={startEdit}
            className="text-[var(--text-meta)] hover:underline"
            style={labelStyle()}
          >
            편집
          </button>
        ) : (
          <div className="flex items-center gap-1">
            <ToolbarButton onClick={() => setEditing(false)} disabled={busy}>
              취소
            </ToolbarButton>
            <ToolbarButton tone="primary" onClick={save} disabled={busy}>
              {busy ? "저장 중…" : "저장"}
            </ToolbarButton>
          </div>
        )}
      </div>

      {error ? (
        <p className="mb-2 text-[var(--text-meta)]" style={{ color: "var(--color-attention, #ef7f62)" }}>
          {error}
        </p>
      ) : null}

      {loading ? (
        <Skeleton className="h-5 w-48" />
      ) : editing ? (
        <div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {draft.map((f) => (
              <div
                key={f.id}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => {
                  reorder(f.id);
                  setDragId(null);
                }}
                className={`rounded-[8px] p-2.5 ${f.multiline ? "sm:col-span-2" : ""} ${
                  dragId === f.id ? "opacity-50" : ""
                }`}
                style={{ border: "1px solid var(--color-divider-soft)", background: "var(--color-card)" }}
              >
                <div className="mb-1.5 flex items-center gap-1.5">
                  <span
                    draggable
                    onDragStart={() => setDragId(f.id)}
                    onDragEnd={() => setDragId(null)}
                    className="shrink-0 cursor-grab select-none px-0.5 text-[var(--text-body-sm)] active:cursor-grabbing"
                    style={{ color: "var(--color-text-faint)" }}
                    title="드래그해서 순서 변경"
                  >
                    ⠿
                  </span>
                  <input
                    value={f.label}
                    placeholder="항목 이름 (예: 주소)"
                    onChange={(e) => updateField(f.id, { label: e.target.value })}
                    className="min-w-0 flex-1 bg-transparent text-[var(--text-body-sm)] font-semibold outline-none"
                    style={{ color: "var(--color-text-strong)" }}
                  />
                  <button
                    type="button"
                    onClick={() => updateField(f.id, { multiline: !f.multiline })}
                    className="shrink-0 rounded-[5px] px-2 py-0.5 text-[var(--text-meta)]"
                    style={{
                      border: "1px solid var(--color-divider)",
                      color: "var(--color-text-muted)",
                    }}
                    title="한 줄 / 여러 줄 전환"
                  >
                    {f.multiline ? "여러 줄" : "한 줄"}
                  </button>
                  <button
                    type="button"
                    onClick={() => removeField(f.id)}
                    className="shrink-0 text-[var(--text-meta)]"
                    style={{ color: "var(--color-fail-fg)" }}
                    title="항목 삭제"
                  >
                    삭제
                  </button>
                </div>
                {f.multiline ? (
                  <TextArea
                    value={f.value}
                    onChange={(e) => updateField(f.id, { value: e.target.value })}
                  />
                ) : (
                  <TextInput
                    value={f.value}
                    onChange={(e) => updateField(f.id, { value: e.target.value })}
                  />
                )}
              </div>
            ))}
          </div>
          <button
            type="button"
            onClick={addField}
            className="mt-2 text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            ＋ 항목 추가
          </button>
        </div>
      ) : visible.length === 0 ? (
        <p className="text-[var(--text-body-sm)]" style={labelStyle()}>
          아직 입력된 정보가 없습니다. 편집을 눌러 추가하세요.
        </p>
      ) : (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2">
          {visible.map((f) => (
            <div
              key={f.id}
              className={`flex flex-col gap-0.5 ${f.multiline ? "sm:col-span-2" : ""}`}
            >
              <dt className="text-[var(--text-meta)] font-semibold" style={labelStyle()}>
                {f.label}
              </dt>
              {f.multiline ? (
                <dd
                  className="rounded-[6px] px-3 py-2 text-[var(--text-body-sm)] whitespace-pre-wrap"
                  style={{ background: "var(--color-panel)", color: "var(--color-text-body)" }}
                >
                  {f.value}
                </dd>
              ) : (
                <dd
                  className="text-[var(--text-body-sm)]"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  {f.value}
                </dd>
              )}
            </div>
          ))}
        </dl>
      )}

      {/* 노션식 자유 섹션: 항목 외 무엇이든 추가(예: "주간 정기 회의 · 일요일 14:00").
          /토글·/페이지(하위 페이지)·표·이미지 등 자유롭게. 항상 편집 가능, 자동 저장. */}
      {!loading && freePageId ? (
        <div className="mt-3">
          <div
            className="mb-1 flex items-center gap-1.5 text-[var(--text-meta)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            <span>＋ 자유 섹션</span>
            <span>
              슬래시(/)로 토글·페이지·표 등 추가 — 예: “주간 정기 회의 · 일요일 14:00”
            </span>
          </div>
          <div
            className="rounded-[8px] px-2 py-1"
            style={{ border: "1px solid var(--color-divider-soft)", background: "var(--color-card)" }}
          >
            <PageStack rootPageId={freePageId} hideRootTitle minHeight={140} />
          </div>
        </div>
      ) : !loading && !editing ? (
        <div className="mt-3">
          <button
            type="button"
            onClick={addFreeSection}
            disabled={addingSection}
            className="text-[var(--text-meta)] hover:underline"
            style={{ color: "var(--color-text-faint)" }}
          >
            {addingSection ? "추가 중…" : "＋ 자유 섹션 추가 (정기 회의·메모 등)"}
          </button>
        </div>
      ) : null}
    </Card>
  );
}
