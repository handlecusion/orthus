"use client";

/**
 * 데이터베이스 셀 값 에디터. 속성 타입별로 알맞은 인라인 에디터를 렌더한다.
 * 표 셀과 행 상세(peek)가 같은 컴포넌트를 공유한다.
 */

import { memo, useId, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ExternalLink, FileIcon, ImageIcon, Video, X } from "lucide-react";
import { API_BASE, api, type DbFileValue, type DbPropertyDef, type UploadProgress } from "@/lib/api";
import {
  DatePicker,
  anchoredStyle,
  memberPillColor,
  TagPill,
  useAnchoredPopover,
} from "@/components/dashboard/kit";
import { OptionSelect, type OptionSelectHandlers } from "./OptionSelect";

export type MemberOption = { id: string; name: string; color: string };

const FILE_VALUE_MAX_BYTES = 50 * 1024 * 1024;

/** 진행 중 업로드 1건의 표시 상태. */
type ActiveUpload = {
  key: string;
  name: string;
  percent: number;
  rateBps: number;
  etaSeconds: number | null;
  abort: () => void;
};

function fmtRate(bps: number): string {
  if (bps >= 1024 * 1024) return `${(bps / 1024 / 1024).toFixed(1)}MB/s`;
  if (bps >= 1024) return `${Math.round(bps / 1024)}KB/s`;
  return bps > 0 ? `${Math.round(bps)}B/s` : "";
}

function fmtEta(seconds: number | null): string {
  if (seconds == null) return "";
  if (seconds < 60) return `${seconds}초 남음`;
  const m = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${m}분 ${rest}초 남음` : `${m}분 남음`;
}

function UploadProgressRow({ upload }: { upload: ActiveUpload }) {
  const meta = [fmtRate(upload.rateBps), fmtEta(upload.etaSeconds)].filter(Boolean).join(" · ");
  return (
    <div
      className="grid gap-1 rounded-[6px] px-2 py-1.5"
      style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider-soft)" }}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span
          className="min-w-0 flex-1 truncate text-[var(--text-meta)] font-semibold"
          style={{ color: "var(--color-text-strong)" }}
        >
          {upload.name}
        </span>
        <span className="shrink-0 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          {upload.percent}%
        </span>
        <button
          type="button"
          aria-label="업로드 취소"
          onClick={upload.abort}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] hover:bg-[var(--color-panel)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          <X size={13} />
        </button>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full" style={{ background: "var(--color-divider-soft)" }}>
        <div
          className="h-full rounded-full transition-[width] duration-200"
          style={{ width: `${upload.percent}%`, background: "var(--color-progress, #2563eb)" }}
        />
      </div>
      {meta ? (
        <div className="text-[10px]" style={{ color: "var(--color-text-muted)" }}>
          {meta}
        </div>
      ) : null}
    </div>
  );
}

function shortFileId(): string {
  const raw =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  return raw.replace(/-/g, "").slice(0, 10);
}

function fileKind(mime: string, name = ""): DbFileValue["type"] {
  const lower = name.toLowerCase();
  if (mime.startsWith("image/") || /\.(png|jpe?g|gif|webp|svg|avif)$/i.test(lower)) return "image";
  if (mime.startsWith("video/") || /\.(mp4|mov|webm|m4v|avi)$/i.test(lower)) return "video";
  return "file";
}

export function normalizeFiles(value: unknown): DbFileValue[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => item != null && typeof item === "object")
    .map((item) => {
      const name = String(item.name || "첨부 파일");
      const mime = typeof item.mime === "string" ? item.mime : "";
      const explicitType =
        item.type === "image" || item.type === "video" || item.type === "file"
          ? item.type
          : fileKind(mime, name);
      return {
        id: typeof item.id === "string" && item.id ? item.id : shortFileId(),
        name,
        type: explicitType,
        mime: mime || null,
        size: typeof item.size === "number" ? item.size : null,
        url: typeof item.url === "string" ? item.url : null,
        dataUrl: typeof item.dataUrl === "string" ? item.dataUrl : null,
        storage:
          item.storage === "database" || item.storage === "external" || item.storage === "inline"
            ? item.storage
            : typeof item.dataUrl === "string"
              ? "inline"
              : typeof item.url === "string" && item.url.startsWith("/dashboard/databases/")
                ? "database"
                : typeof item.url === "string"
                  ? "external"
                  : null,
      };
    });
}

export function normalizePeople(value: unknown): string[] {
  if (typeof value === "string" && value) return [value];
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(value.filter((item): item is string => typeof item === "string" && item.length > 0)),
  );
}

function formatFileSize(size: number | null | undefined): string {
  if (!size || !Number.isFinite(size)) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function fileSource(file: DbFileValue): string {
  const raw = file.dataUrl || file.url || "";
  if (!raw) return "";
  if (/^(https?:|data:|blob:)/i.test(raw)) return raw;
  if (raw.startsWith("/")) return `${API_BASE}${raw}`;
  return raw;
}

/** 노션 협업업무표 카드의 담당자 아바타(색 원 + 이니셜). */
export function RowAvatar({ name, color }: { name: string; color: string }) {
  const c = memberPillColor(color);
  return (
    <span
      className="inline-flex h-[18px] w-[18px] items-center justify-center rounded-full text-[10px] font-semibold"
      style={{ background: c.bg, color: c.fg }}
      title={name}
    >
      {(name || "?").trim().charAt(0)}
    </span>
  );
}

/** created_time/last_edited_time 표시용 행 메타. */
export type RowMeta = { createdAt?: string | null; updatedAt?: string | null };

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    // 표 컬럼이 한 줄에 들어가도록 compact: 2026.06.28 22:48
    const d = new Date(iso);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  } catch {
    return "—";
  }
}

function PropValueInner({
  prop,
  value,
  onChange,
  handlers,
  members,
  meta,
  databaseId,
  rowId,
  fill = false,
}: {
  prop: DbPropertyDef;
  value: unknown;
  onChange: (next: unknown) => void;
  handlers: OptionSelectHandlers;
  members: MemberOption[];
  meta?: RowMeta;
  databaseId?: string;
  rowId?: string;
  fill?: boolean;
}) {
  switch (prop.type) {
    case "created_time":
    case "last_edited_time":
      return (
        <span
          className="px-1.5 py-1 text-[var(--text-body-sm)] tabular-nums"
          style={{ color: "var(--color-text-muted)" }}
        >
          {fmtDateTime(prop.type === "created_time" ? meta?.createdAt : meta?.updatedAt)}
        </span>
      );
    case "select":
    case "status":
      return (
        <OptionSelect
          prop={prop}
          value={typeof value === "string" ? value : null}
          multi={false}
          onChange={(v) => onChange(v)}
          handlers={handlers}
          fill={fill}
        />
      );
    case "multi_select":
      return (
        <OptionSelect
          prop={prop}
          value={Array.isArray(value) ? (value as string[]) : []}
          multi
          onChange={(v) => onChange(v)}
          handlers={handlers}
          fill={fill}
        />
      );
    case "date":
      return (
        <DatePicker
          value={typeof value === "string" ? value : ""}
          onChange={(v) => onChange(v || null)}
        />
      );
    case "checkbox":
      return (
        <input
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
          className="h-[18px] w-[18px] cursor-pointer accent-[var(--color-text-strong)]"
        />
      );
    case "number":
      return (
        <TextCell
          value={value == null ? "" : String(value)}
          numeric
          onCommit={(v) => onChange(v === "" ? null : Number(v))}
          fill={fill}
        />
      );
    case "person":
      return (
        <PersonSelect
          members={members}
          value={normalizePeople(value)}
          onChange={(v) => onChange(v)}
          fill={fill}
        />
      );
    case "url":
      return (
        <TextCell
          value={typeof value === "string" ? value : ""}
          onCommit={(v) => onChange(v || null)}
          link
          fill={fill}
        />
      );
    case "files":
      return (
        <FilesCell
          value={normalizeFiles(value)}
          onChange={(v) => onChange(v.length ? v : null)}
          databaseId={databaseId}
          rowId={rowId}
          fill={fill}
        />
      );
    default:
      // title / text
      return (
        <TextCell
          value={typeof value === "string" ? value : ""}
          onCommit={(v) => onChange(v || null)}
          fill={fill}
          bold={prop.type === "title"}
          placeholder={prop.type === "title" ? "제목 없음" : ""}
        />
      );
  }
}

/** 셀 단위 memo — 표/보드가 행×속성 개수만큼 렌더하므로 무관한 행 변경에 재렌더하지
 *  않게 한다. 계약: onChange/handlers는 렌더마다 새 객체지만 동작이
 *  (databaseId, rowId, prop.id) + 컴포넌트-수명 안정 콜백에만 의존해야 하며
 *  (현재 호출측 모두 해당) 비교에서 의도적으로 제외한다 — databaseId/rowId/prop
 *  비교가 값 동일성을 대신한다. meta는 객체가 렌더마다 새로 만들어져 필드로 비교한다. */
export const PropValue = memo(
  PropValueInner,
  (prev, next) =>
    prev.prop === next.prop &&
    prev.value === next.value &&
    prev.members === next.members &&
    prev.databaseId === next.databaseId &&
    prev.rowId === next.rowId &&
    prev.fill === next.fill &&
    prev.meta?.createdAt === next.meta?.createdAt &&
    prev.meta?.updatedAt === next.meta?.updatedAt,
);

function TextCell({
  value,
  onCommit,
  numeric = false,
  link = false,
  bold = false,
  fill = false,
  placeholder = "",
}: {
  value: string;
  onCommit: (v: string) => void;
  numeric?: boolean;
  link?: boolean;
  bold?: boolean;
  fill?: boolean;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState(value);
  const [focused, setFocused] = useState(false);
  // 외부 value 변경을 렌더 중 동기화(포커스 중에는 사용자 입력 보존). React 권장
  // "이전 prop 추적" 패턴 — effect에서 setState하지 않는다.
  const [seenValue, setSeenValue] = useState(value);
  if (value !== seenValue) {
    setSeenValue(value);
    if (!focused) setDraft(value);
  }

  const skip = useRef(false); // Escape 취소: blur 커밋을 한 번 건너뛴다.

  const commit = () => {
    setFocused(false);
    if (skip.current) {
      skip.current = false;
      setDraft(value);
      return;
    }
    if (draft !== value) onCommit(draft.trim());
  };

  return (
    <span className="flex items-center gap-1" style={{ width: fill ? "100%" : undefined }}>
      <input
        value={draft}
        inputMode={numeric ? "decimal" : undefined}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          else if (e.key === "Escape") {
            skip.current = true;
            (e.target as HTMLInputElement).blur();
          }
        }}
        className="min-w-0 flex-1 rounded-[6px] bg-transparent px-1.5 py-1 text-[var(--text-body-sm)] outline-none focus:bg-[var(--color-app-canvas)]"
        style={{
          width: fill ? "100%" : 140,
          color: "var(--color-text-strong)",
          fontWeight: bold ? 600 : 400,
        }}
      />
      {link && value && !focused ? (
        <a
          href={value.startsWith("http") ? value : `https://${value}`}
          target="_blank"
          rel="noreferrer noopener"
          onClick={(e) => e.stopPropagation()}
          className="shrink-0 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-faint)" }}
          title="링크 열기"
        >
          ↗
        </a>
      ) : null}
    </span>
  );
}

export function FileStrip({
  files,
  compact = false,
}: {
  files: DbFileValue[];
  compact?: boolean;
}) {
  if (!files.length) return null;
  const shown = files.slice(0, compact ? 3 : 6);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1">
      {shown.map((file) => {
        const src = fileSource(file);
        return (
          <span
            key={file.id}
            className="inline-flex min-w-0 items-center gap-1 rounded-[6px] px-1.5 py-1 text-[var(--text-meta)]"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }}
            title={file.name}
          >
            {file.type === "image" && src ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={src}
                alt=""
                className="h-5 w-7 rounded-[4px] object-cover"
                draggable={false}
              />
            ) : (
              <span aria-hidden>{file.type === "video" ? "🎬" : "📎"}</span>
            )}
            <span className={compact ? "max-w-[70px] truncate" : "max-w-[140px] truncate"}>
              {file.name}
            </span>
          </span>
        );
      })}
      {files.length > shown.length ? (
        <span
          className="rounded-[6px] px-1.5 py-1 text-[var(--text-meta)]"
          style={{ background: "var(--color-app-canvas)", color: "var(--color-text-faint)" }}
        >
          +{files.length - shown.length}
        </span>
      ) : null}
    </div>
  );
}

export function MediaCover({ files }: { files: DbFileValue[] }) {
  const media = files.find((file) => file.type === "image" || file.type === "video");
  const src = media ? fileSource(media) : "";
  if (!media || !src) return null;
  return (
    <div
      className="mb-2 overflow-hidden rounded-[7px]"
      style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider-soft)" }}
    >
      {media.type === "image" ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={media.name} className="h-[116px] w-full object-cover" draggable={false} />
      ) : (
        <video src={src} className="h-[116px] w-full object-cover" muted playsInline preload="metadata" />
      )}
    </div>
  );
}

export function MediaGallery({ files }: { files: DbFileValue[] }) {
  const media = files.filter((file) => file.type === "image" || file.type === "video");
  if (!media.length) return null;
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {media.map((file) => {
        const src = fileSource(file);
        if (!src) return null;
        return (
          <figure
            key={file.id}
            className="overflow-hidden rounded-[10px]"
            style={{ border: "1px solid var(--color-divider-soft)", background: "var(--color-app-canvas)" }}
          >
            {file.type === "image" ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={src} alt={file.name} className="max-h-[280px] w-full object-contain" draggable={false} />
            ) : (
              <video src={src} controls className="max-h-[280px] w-full bg-black" preload="metadata" />
            )}
            <figcaption
              className="truncate px-2 py-1.5 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {file.name}
            </figcaption>
          </figure>
        );
      })}
    </div>
  );
}

function FilesCell({
  value,
  onChange,
  databaseId,
  rowId,
  fill = false,
}: {
  value: DbFileValue[];
  onChange: (v: DbFileValue[]) => void;
  databaseId?: string;
  rowId?: string;
  fill?: boolean;
}) {
  const inputId = useId();
  const [busy, setBusy] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [urlDraft, setUrlDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  // 진행 중 업로드(파일별 %·속도·남은시간·취소). 완료/취소 시 목록에서 제거.
  const [uploads, setUploads] = useState<ActiveUpload[]>([]);

  async function addFiles(files: FileList | File[] | null) {
    const picked = files ? Array.from(files) : [];
    if (!picked.length) return;
    setBusy(true);
    setError(null);
    try {
      const next: DbFileValue[] = [];
      for (const file of picked) {
        // 동영상·대용량은 디스크 미디어 스토어로 청크 업로드(스트리밍 재생 +
        // Cloudflare 바디 상한 우회). 그 외 문서/이미지는 기존 DB 저장 유지.
        const useMedia = file.type.startsWith("video/") || file.size > FILE_VALUE_MAX_BYTES;
        if (!useMedia && (!databaseId || !rowId)) {
          setError("저장할 행을 먼저 만든 뒤 파일을 첨부할 수 있습니다.");
          continue;
        }
        const controller = new AbortController();
        const key = `${Date.now()}-${file.name}-${Math.random().toString(36).slice(2, 7)}`;
        const onProgress = (progress: UploadProgress) =>
          setUploads((prev) =>
            prev.map((u) =>
              u.key === key
                ? {
                    ...u,
                    percent: progress.percent,
                    rateBps: progress.rateBps,
                    etaSeconds: progress.etaSeconds,
                  }
                : u,
            ),
          );
        setUploads((prev) => [
          ...prev,
          {
            key,
            name: file.name,
            percent: 0,
            rateBps: 0,
            etaSeconds: null,
            abort: () => controller.abort(),
          },
        ]);
        try {
          if (useMedia) {
            const asset = await api.uploadDashboardMediaChunked(file, {
              onProgress,
              signal: controller.signal,
            });
            next.push({
              id: shortFileId(),
              name: file.name,
              type: fileKind(file.type || "", file.name),
              url: asset.url,
              dataUrl: null,
              mime: file.type || null,
              size: file.size,
              storage: "external",
            });
          } else if (databaseId && rowId) {
            next.push(
              await api.uploadProjectDatabaseFile(databaseId, rowId, file, {
                onProgress,
                signal: controller.signal,
              }),
            );
          }
        } catch (e) {
          if (e instanceof DOMException && e.name === "AbortError") {
            // 사용자가 취소 — 조용히 넘어간다.
          } else {
            setError(e instanceof Error ? e.message : "파일을 업로드하지 못했습니다.");
          }
        } finally {
          setUploads((prev) => prev.filter((u) => u.key !== key));
        }
      }
      if (next.length) onChange([...value, ...next]);
    } finally {
      setBusy(false);
    }
  }

  function addUrl() {
    const url = urlDraft.trim();
    if (!url) return;
    const cleanUrl = /^https?:\/\//i.test(url) ? url : `https://${url}`;
    const name = cleanUrl.split("/").filter(Boolean).pop()?.split("?")[0] || cleanUrl;
    onChange([
      ...value,
      {
        id: shortFileId(),
        name,
        type: fileKind("", name),
        url: cleanUrl,
        dataUrl: null,
        mime: null,
        size: null,
        storage: "external",
      },
    ]);
    setUrlDraft("");
  }

  function remove(id: string) {
    const file = value.find((item) => item.id === id);
    if (databaseId && file?.storage === "database") {
      void api.deleteProjectDatabaseFile(databaseId, id).catch(() => {});
    }
    onChange(value.filter((file) => file.id !== id));
  }

  function moveFile(id: string, direction: -1 | 1) {
    const idx = value.findIndex((file) => file.id === id);
    const nextIdx = idx + direction;
    if (idx < 0 || nextIdx < 0 || nextIdx >= value.length) return;
    const next = [...value];
    const [file] = next.splice(idx, 1);
    next.splice(nextIdx, 0, file);
    onChange(next);
  }

  return (
    <div
      data-db-file-dropzone=""
      className="grid gap-1.5 rounded-[8px] p-1 transition-colors"
      style={{
        width: fill ? "100%" : 260,
        border: dragActive ? "1px dashed var(--color-text-muted)" : "1px solid transparent",
        background: dragActive ? "var(--color-app-canvas)" : "transparent",
      }}
      onDragEnter={(event) => {
        event.preventDefault();
        event.stopPropagation();
        setDragActive(true);
      }}
      onDragOver={(event) => {
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = "copy";
        setDragActive(true);
      }}
      onDragLeave={(event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setDragActive(false);
        }
      }}
      onDrop={(event) => {
        event.preventDefault();
        event.stopPropagation();
        setDragActive(false);
        void addFiles(event.dataTransfer.files);
      }}
    >
      <FileStrip files={value} />
      {uploads.map((u) => (
        <UploadProgressRow key={u.key} upload={u} />
      ))}
      <div className="flex flex-wrap items-center gap-1">
        <input
          id={inputId}
          type="file"
          multiple
          accept="image/*,video/*,.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.csv,.zip,.txt"
          className="sr-only"
          onChange={(event) => {
            void addFiles(event.target.files);
            event.currentTarget.value = "";
          }}
        />
        <label
          htmlFor={inputId}
          className="inline-flex min-h-[32px] cursor-pointer items-center rounded-[6px] px-2 text-[var(--text-meta)] font-semibold hover:bg-[var(--color-app-canvas)]"
          style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
        >
          {busy ? "업로드 중" : "+ 파일"}
        </label>
        <input
          value={urlDraft}
          onChange={(event) => setUrlDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addUrl();
            }
          }}
          placeholder="URL"
          className="min-h-[32px] min-w-[120px] flex-1 rounded-[6px] px-2 text-[var(--text-meta)] outline-none"
          style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
        />
        <button
          type="button"
          onClick={addUrl}
          className="min-h-[32px] rounded-[6px] px-2 text-[var(--text-meta)] font-semibold hover:bg-[var(--color-app-canvas)]"
          style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
        >
          추가
        </button>
      </div>
      {value.length ? (
        <div className="grid gap-1">
          {value.map((file, index) => {
            const src = fileSource(file);
            return (
              <div
                key={file.id}
                className="flex min-h-[30px] min-w-0 items-center gap-2 rounded-[6px] px-2"
                style={{ background: "var(--color-app-canvas)" }}
              >
                <span
                  className="flex h-9 w-11 shrink-0 items-center justify-center overflow-hidden rounded-[5px]"
                  style={{
                    background: "var(--color-panel)",
                    border: "1px solid var(--color-divider-soft)",
                    color: "var(--color-text-faint)",
                  }}
                  aria-hidden="true"
                >
                  {file.type === "image" && src ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={src}
                      alt=""
                      className="h-full w-full object-cover"
                      draggable={false}
                    />
                  ) : file.type === "video" && src ? (
                    <video
                      src={src}
                      className="h-full w-full object-cover"
                      muted
                      playsInline
                      preload="metadata"
                    />
                  ) : file.type === "image" ? (
                    <ImageIcon size={15} strokeWidth={2} />
                  ) : file.type === "video" ? (
                    <Video size={15} strokeWidth={2} />
                  ) : (
                    <FileIcon size={15} strokeWidth={2} />
                  )}
                </span>
                <span
                  className="min-w-0 flex-1 truncate text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-body)" }}
                  title={file.name}
                >
                  {file.name}
                </span>
                {formatFileSize(file.size) ? (
                  <span
                    className="shrink-0 text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-faint)" }}
                  >
                    {formatFileSize(file.size)}
                  </span>
                ) : null}
                <button
                  type="button"
                  onClick={() => moveFile(file.id, -1)}
                  disabled={index === 0}
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)] disabled:cursor-not-allowed disabled:opacity-35"
                  style={{ color: "var(--color-text-muted)" }}
                  title="위로 이동"
                  aria-label={`${file.name} 위로 이동`}
                  data-db-file-move="up"
                >
                  <ArrowUp size={13} strokeWidth={2} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => moveFile(file.id, 1)}
                  disabled={index === value.length - 1}
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)] disabled:cursor-not-allowed disabled:opacity-35"
                  style={{ color: "var(--color-text-muted)" }}
                  title="아래로 이동"
                  aria-label={`${file.name} 아래로 이동`}
                  data-db-file-move="down"
                >
                  <ArrowDown size={13} strokeWidth={2} aria-hidden="true" />
                </button>
                {src ? (
                  <a
                    href={src}
                    target="_blank"
                    rel="noreferrer noopener"
                    onClick={(event) => event.stopPropagation()}
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] text-[var(--text-meta)] hover:bg-[var(--color-divider-soft)]"
                    style={{ color: "var(--color-text-muted)" }}
                    title="열기"
                  >
                    <ExternalLink size={13} strokeWidth={2} aria-hidden="true" />
                  </a>
                ) : null}
                <button
                  type="button"
                  onClick={() => remove(file.id)}
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] text-[var(--text-meta)] hover:bg-[var(--color-divider-soft)]"
                  style={{ color: "var(--color-fail-fg)" }}
                  title="삭제"
                  aria-label={`${file.name} 삭제`}
                >
                  <X size={13} strokeWidth={2} aria-hidden="true" />
                </button>
              </div>
            );
          })}
        </div>
      ) : null}
      {error ? (
        <p className="text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>
          {error}
        </p>
      ) : null}
    </div>
  );
}

function PersonSelect({
  members,
  value,
  onChange,
  fill = false,
}: {
  members: MemberOption[];
  value: string[];
  onChange: (v: string[] | null) => void;
  fill?: boolean;
}) {
  const { anchorRef, popRef, rect, open, toggle } = useAnchoredPopover();
  const selected = value
    .map((id) => members.find((m) => m.id === id))
    .filter((m): m is MemberOption => !!m);
  const selectedIds = new Set(value);
  const setNext = (ids: string[]) => onChange(ids.length ? ids : null);
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        className="flex min-h-[28px] flex-wrap items-center gap-1 rounded-[6px] px-1.5 py-0.5 text-left hover:bg-[var(--color-app-canvas)]"
        style={{ width: fill ? "100%" : undefined }}
      >
        {selected.length ? (
          selected.map((m) => (
            <TagPill key={m.id} label={m.name} color={memberPillColor(m.color)} />
          ))
        ) : (
          <span
            className="text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            비어 있음
          </span>
        )}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 overflow-y-auto rounded-[10px] p-1.5"
          style={{
            ...anchoredStyle(rect, 220, 300),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {selected.length ? (
            <button
              type="button"
              onClick={() => {
                onChange(null);
              }}
              className="mb-1 flex min-h-[28px] w-full items-center rounded-[6px] px-2 text-left text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              비우기
            </button>
          ) : null}
          {members.length === 0 ? (
            <p
              className="px-2 py-1 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-faint)" }}
            >
              팀 멤버가 없습니다.
            </p>
          ) : (
            members.map((m) => (
              <button
                key={m.id}
                type="button"
                onClick={() => {
                  setNext(
                    selectedIds.has(m.id)
                      ? value.filter((id) => id !== m.id)
                      : [...value, m.id],
                  );
                }}
                className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-1.5 text-left hover:bg-[var(--color-app-canvas)]"
              >
                <span className="w-4 text-center" style={{ color: "var(--color-text-faint)" }}>
                  {selectedIds.has(m.id) ? "✓" : ""}
                </span>
                <TagPill label={m.name} color={memberPillColor(m.color)} />
              </button>
            ))
          )}
        </div>
      ) : null}
    </>
  );
}
