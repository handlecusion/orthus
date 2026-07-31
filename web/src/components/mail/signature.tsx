"use client";

/**
 * 메일 명함(서명) 편집 모달 + HTML 렌더.
 *
 * "메일 명함" 버튼을 누르면 본인 비즈니스 카드(이름/직함/회사/이메일/전화/웹사이트 +
 * LinkedIn 등 외부 링크)를 편집한다. 저장(`PUT /mail/signature`, owner-scope)하거나
 * 현재 값을 작성 중인 메일 본문에 styled HTML로 삽입한다.
 *
 * 서버는 구조화 필드만 저장하고 HTML은 FE가 만든다(저장 XSS 차단). 삽입 HTML은
 * 사용자 입력을 전부 escape한 inline-style 카드라 메일 클라이언트 호환성이 좋다.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { api, type MailSignature, type MailSignatureLink } from "@/lib/api";
import { Banner, ToolbarButton, cx, inputClass, inputStyle } from "@/components/ui";

const EMPTY: MailSignature = {
  from_addr: "",
  display_name: "",
  title: "",
  company: "",
  email: "",
  phone: "",
  website: "",
  links: [],
  enabled: true,
};

const SIGNATURE_BLOCK_RE = /(?:<br\s*\/?>\s*)?<table\b[^>]*data-orthus-mail-signature="true"[\s\S]*?<\/table>/gi;

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(value: string): string {
  // http(s)만 허용(스킴 인젝션 차단). 그 외는 빈 링크로.
  if (!/^https?:\/\//i.test(value)) return "";
  return escapeHtml(value);
}

/** 명함 필드 → 메일 본문에 넣을 styled HTML 카드. */
export function renderSignatureHtml(sig: MailSignature): string {
  if (!sig.enabled) return "";
  const name = escapeHtml(sig.display_name.trim());
  const roleParts = [sig.title, sig.company].map((p) => escapeHtml(p.trim())).filter(Boolean);
  const contactParts = [sig.email, sig.phone].map((p) => escapeHtml(p.trim())).filter(Boolean);
  const website = escapeAttr(sig.website.trim());
  const links = sig.links
    .map((link) => ({ label: escapeHtml(link.label.trim()), url: escapeAttr(link.url.trim()) }))
    .filter((link) => link.url);

  const rows: string[] = [];
  if (name) rows.push(`<div style="font-weight:600;font-size:14px;color:#111827">${name}</div>`);
  if (roleParts.length)
    rows.push(`<div style="color:#6b7280">${roleParts.join(" · ")}</div>`);
  if (contactParts.length) rows.push(`<div>${contactParts.join(" · ")}</div>`);
  if (website)
    rows.push(
      `<div><a href="${website}" style="color:#2563eb;text-decoration:none">${website}</a></div>`,
    );
  if (links.length) {
    const anchors = links
      .map(
        (link) =>
          `<a href="${link.url}" style="color:#2563eb;text-decoration:none">${link.label || link.url}</a>`,
      )
      .join(" · ");
    rows.push(`<div>${anchors}</div>`);
  }
  if (!rows.length) return "";

  return (
    `<br><table data-orthus-mail-signature="true" data-orthus-from="${escapeHtml(sig.from_addr.trim())}" ` +
    `cellpadding="0" cellspacing="0" role="presentation" style="margin-top:12px;padding-top:10px;` +
    `border-top:1px solid #e5e7eb;font-family:-apple-system,'Segoe UI',sans-serif;color:#374151;` +
    `font-size:13px;line-height:1.6"><tbody><tr><td>` +
    rows.join("") +
    `</td></tr></tbody></table>`
  );
}

export function mailSignatureHasContent(sig: MailSignature): boolean {
  return Boolean(
    sig.display_name ||
      sig.title ||
      sig.company ||
      sig.email ||
      sig.phone ||
      sig.website ||
      sig.links.some((link) => link.label || link.url),
  );
}

export function removeMailSignatureHtml(html: string): string {
  return html.replace(SIGNATURE_BLOCK_RE, "").trim();
}

export function applyMailSignatureHtml(html: string, sig: MailSignature | null): string {
  const withoutSignature = removeMailSignatureHtml(html);
  if (!sig || !sig.enabled || !mailSignatureHasContent(sig)) return withoutSignature;
  const signatureHtml = renderSignatureHtml(sig);
  if (!signatureHtml) return withoutSignature;

  const quoteMatch = withoutSignature.match(/(?:<br\s*\/?>\s*){1,2}<div\b[^>]*data-orthus-reply-quote="true"/i);
  if (quoteMatch?.index != null) {
    return `${withoutSignature.slice(0, quoteMatch.index)}${signatureHtml}${withoutSignature.slice(quoteMatch.index)}`;
  }
  return `${withoutSignature}${signatureHtml}`;
}

export function MailSignatureModal({
  open,
  onClose,
  onInsert,
  fromAddr,
  mailboxes,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  /** 현재 명함을 메일 본문에 삽입(렌더된 HTML 전달). */
  onInsert: (html: string) => void;
  fromAddr: string;
  /** 관리 가능한 발신 메일함 목록 — 있으면 메일함별 명함을 전환해 편집한다. */
  mailboxes?: string[];
  onSaved?: (signature: MailSignature) => void;
}) {
  const [sig, setSig] = useState<MailSignature>(EMPTY);
  // 메일함별 명함 관리: 모달 안에서 다른 메일함(2fe@nova.example / biz@… 등)으로
  // 전환해 각 명함을 따로 보고 수정한다. 기본은 작성창의 보내는 사람.
  const [activeAddr, setActiveAddr] = useState(fromAddr);
  // 로드 완료된 메일함 주소 — activeAddr와 다르면 로딩 중(파생 상태라 effect 안
  // 동기 setState 없이 메일함 전환마다 로딩 표시가 된다).
  const [loadedAddr, setLoadedAddr] = useState<string | null>(null);
  const loading = loadedAddr !== activeAddr;
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ tone: "pass" | "fail"; message: string } | null>(null);

  // 메일함 전환마다 서버를 다시 불러오면 저장 안 한 편집이 사라진다 — 주소별로
  // "지금까지 편집한 값"을 이 모달 인스턴스 생명주기 동안 보관해 되돌아오면
  // 그대로 복원한다. 함께 보관하는 baseline은 그 주소의 서버 원본(또는 마지막
  // 저장값)이라 dirty(닫기 확인) 판정 기준이 된다.
  const editsRef = useRef<Map<string, MailSignature>>(new Map());
  const baselinesRef = useRef<Map<string, MailSignature>>(new Map());

  const addrChoices = useMemo(() => {
    const list = [fromAddr, ...(mailboxes ?? [])].map((a) => (a || "").trim()).filter(Boolean);
    return [...new Set(list)];
  }, [fromAddr, mailboxes]);

  // 보내는 사람이 바뀌면 부모가 key={fromAddr}로 리마운트해 activeAddr 초기값을
  // 새 보내는 사람으로 되돌린다(effect 내 동기 setState 회피).

  // 선택된 메일함의 명함을 불러온다 — 이 세션에서 이미 편집(또는 저장)한 적이
  // 있는 메일함이면(editsRef에 항목이 있으면) 재조회 없이 건너뛴다. 그 복원은
  // switchAddr가 전환 이벤트 핸들러에서 동기로 처리하므로, 여기서는 처음 보는
  // 메일함만 서버에서 가져온다. 동기 setState 없이 async 콜백에서만 상태를
  // 바꿔 react-hooks/set-state-in-effect를 피한다(메일 페이지 패턴 동형).
  useEffect(() => {
    if (!open) return;
    if (editsRef.current.has(activeAddr)) return;
    let cancelled = false;
    (async () => {
      try {
        const loaded = await api.getMailSignature(activeAddr);
        if (cancelled) return;
        const next: MailSignature = {
          ...EMPTY,
          ...loaded,
          from_addr: activeAddr || loaded.from_addr || "",
          email: loaded.email || activeAddr || "",
          links: loaded.links ?? [],
        };
        baselinesRef.current.set(activeAddr, next);
        setSig(next);
        setNotice(null);
      } catch {
        if (!cancelled) {
          const next: MailSignature = { ...EMPTY, from_addr: activeAddr, email: activeAddr };
          baselinesRef.current.set(activeAddr, next);
          setSig(next);
        }
      } finally {
        if (!cancelled) setLoadedAddr(activeAddr);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeAddr, open]);

  if (!open) return null;

  // 다른 메일함으로 넘어가기 전에 지금 편집 중인 값을 이 메일함 몫으로 남겨두고,
  // 넘어가는 메일함에 이미 저장해 둔 편집값이 있으면(재방문) 재조회 없이 그
  // 자리에서 바로 복원한다 — 이벤트 핸들러라 동기 setState도 안전하다.
  function switchAddr(nextAddr: string) {
    if (nextAddr === activeAddr) return;
    editsRef.current.set(activeAddr, sig);
    const stashed = editsRef.current.get(nextAddr);
    if (stashed) {
      setSig(stashed);
      setLoadedAddr(nextAddr);
      setNotice(null);
    }
    setActiveAddr(nextAddr);
  }

  function isDirty(): boolean {
    const baseline = baselinesRef.current.get(activeAddr);
    if (!baseline) return false;
    return JSON.stringify(sig) !== JSON.stringify(baseline);
  }

  function requestClose() {
    if (isDirty() && !window.confirm("저장하지 않은 변경사항이 있습니다. 닫을까요?")) {
      return;
    }
    onClose();
  }

  const set = <K extends keyof MailSignature>(key: K, value: MailSignature[K]) =>
    setSig((prev) => ({ ...prev, [key]: value }));

  const setLink = (index: number, patch: Partial<MailSignatureLink>) =>
    setSig((prev) => ({
      ...prev,
      links: prev.links.map((link, i) => (i === index ? { ...link, ...patch } : link)),
    }));

  const addLink = () =>
    setSig((prev) =>
      prev.links.length >= 8 ? prev : { ...prev, links: [...prev.links, { label: "", url: "" }] },
    );

  const removeLink = (index: number) =>
    setSig((prev) => ({ ...prev, links: prev.links.filter((_, i) => i !== index) }));

  async function save(): Promise<MailSignature | null> {
    setSaving(true);
    setNotice(null);
    try {
      const cleaned: MailSignature = {
        ...sig,
        from_addr: activeAddr || sig.from_addr,
        links: sig.links.filter((link) => link.label.trim() || link.url.trim()),
      };
      const saved = await api.saveMailSignature(cleaned, activeAddr);
      const next: MailSignature = { ...EMPTY, ...saved, links: saved.links ?? [] };
      setSig(next);
      // 저장한 값이 이 메일함의 새 기준선 — 이후 dirty 판정과 메일함 재전환
      // 복원 모두 이 값부터 다시 시작한다.
      baselinesRef.current.set(activeAddr, next);
      editsRef.current.set(activeAddr, next);
      onSaved?.(saved);
      setNotice({ tone: "pass", message: "명함을 저장했어요" });
      return saved;
    } catch (e) {
      setNotice({ tone: "fail", message: e instanceof Error ? e.message : "저장 실패" });
      return null;
    } finally {
      setSaving(false);
    }
  }

  function insert() {
    if (!sig.enabled) {
      setNotice({ tone: "fail", message: "자동 포함을 켠 뒤 삽입하세요" });
      return;
    }
    if (!mailSignatureHasContent(sig)) {
      setNotice({ tone: "fail", message: "명함 내용을 먼저 입력하세요" });
      return;
    }
    onInsert(renderSignatureHtml(sig));
    onClose();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.4)" }}
      onClick={requestClose}
      role="presentation"
    >
      <div
        className="flex max-h-[90vh] w-full max-w-[420px] flex-col overflow-hidden rounded-[12px]"
        style={{ background: "var(--color-surface,#fff)", border: "1px solid var(--color-divider)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="flex h-12 shrink-0 items-center justify-between px-4"
          style={{ borderBottom: "1px solid var(--color-divider)" }}
        >
          <span
            className="text-[var(--text-body-sm)] font-semibold"
            style={{ color: "var(--color-text-strong)" }}
          >
            메일 명함
          </span>
          <button
            aria-label="닫기"
            className="flex min-h-[44px] min-w-[44px] items-center justify-center"
            onClick={requestClose}
            type="button"
          >
            <X className="h-4 w-4" style={{ color: "var(--color-text-muted)" }} />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 py-4">
          {notice ? (
            <Banner tone={notice.tone} title={notice.tone === "pass" ? "완료" : "오류"}>
              {notice.message}
            </Banner>
          ) : null}
          {loading ? (
            <div className="py-6 text-center text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              불러오는 중…
            </div>
          ) : (
            <>
              {addrChoices.length > 1 ? (
                <label className="grid gap-1">
                  <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
                    메일함별 명함 — 선택한 메일함의 명함을 보고 수정합니다
                  </span>
                  <select
                    aria-label="명함 메일함 선택"
                    className={cx(inputClass, "min-h-[44px]")}
                    onChange={(event) => switchAddr(event.target.value)}
                    style={inputStyle}
                    value={activeAddr}
                  >
                    {addrChoices.map((addr) => (
                      <option key={addr} value={addr}>
                        {addr}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <div
                  className="rounded-[8px] px-3 py-2 text-[var(--text-meta)]"
                  style={{ background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }}
                >
                  적용 메일함: <strong style={{ color: "var(--color-text-strong)" }}>{activeAddr || "기본"}</strong>
                </div>
              )}
              <label className="flex min-h-[40px] items-center gap-2">
                <input
                  type="checkbox"
                  className="h-4 w-4"
                  checked={sig.enabled}
                  onChange={(e) => set("enabled", e.target.checked)}
                />
                <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
                  이 메일함으로 보낼 때 명함 자동 포함
                </span>
              </label>
              <Field label="이름" value={sig.display_name} onChange={(v) => set("display_name", v)} />
              <div className="grid grid-cols-2 gap-2">
                <Field label="직함" value={sig.title} onChange={(v) => set("title", v)} />
                <Field label="회사" value={sig.company} onChange={(v) => set("company", v)} />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label="이메일" value={sig.email} onChange={(v) => set("email", v)} />
                <Field label="전화" value={sig.phone} onChange={(v) => set("phone", v)} />
              </div>
              <Field
                label="웹사이트"
                value={sig.website}
                onChange={(v) => set("website", v)}
                placeholder="https://example.com"
              />

              <div className="grid gap-2">
                <div className="flex items-center justify-between">
                  <span
                    className="text-[var(--text-meta)] font-semibold"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    링크 (LinkedIn 등)
                  </span>
                  <button
                    type="button"
                    onClick={addLink}
                    disabled={sig.links.length >= 8}
                    className="flex min-h-[32px] items-center gap-1 rounded-[6px] px-2 text-[var(--text-meta)] disabled:opacity-40"
                    style={{ color: "var(--color-progress,#2563eb)" }}
                  >
                    <Plus size={14} /> 추가
                  </button>
                </div>
                {sig.links.map((link, index) => (
                  <div key={index} className="flex items-center gap-2">
                    <input
                      className={cx(inputClass, "min-h-[40px] w-[88px] shrink-0")}
                      style={inputStyle}
                      placeholder="LinkedIn"
                      value={link.label}
                      onChange={(e) => setLink(index, { label: e.target.value })}
                    />
                    <input
                      className={cx(inputClass, "min-h-[40px] flex-1")}
                      style={inputStyle}
                      placeholder="https://linkedin.com/in/…"
                      value={link.url}
                      onChange={(e) => setLink(index, { url: e.target.value })}
                    />
                    <button
                      type="button"
                      aria-label="링크 삭제"
                      onClick={() => removeLink(index)}
                      className="flex min-h-[40px] min-w-[40px] items-center justify-center"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                ))}
              </div>
              <div
                className="rounded-[8px] p-3"
                style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider)" }}
              >
                <div className="mb-2 text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
                  발송 미리보기
                </div>
                {mailSignatureHasContent(sig) ? (
                  <div dangerouslySetInnerHTML={{ __html: renderSignatureHtml(sig).replace(/^<br>/, "") }} />
                ) : (
                  <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                    이름, 회사, 이메일 중 하나를 입력하면 여기에 미리 보입니다.
                  </p>
                )}
              </div>
            </>
          )}
        </div>

        <div
          className="flex shrink-0 items-center justify-end gap-2 px-4 py-3"
          style={{ borderTop: "1px solid var(--color-divider)" }}
        >
          <ToolbarButton className="min-h-[44px]" onClick={() => void save()} disabled={saving || loading} type="button">
            {saving ? "저장 중" : "저장"}
          </ToolbarButton>
          <ToolbarButton className="min-h-[44px]" onClick={insert} disabled={loading} type="button">
            본문에 삽입
          </ToolbarButton>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="grid gap-1">
      <span
        className="text-[var(--text-meta)] font-semibold"
        style={{ color: "var(--color-text-muted)" }}
      >
        {label}
      </span>
      <input
        className={cx(inputClass, "min-h-[40px]")}
        style={inputStyle}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}
