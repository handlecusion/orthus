"use client";

/**
 * 테마 설정 (/settings/appearance). 세 섹션:
 * 1. 기본 테마 — 3-way segmented control(라이트/다크/시스템). localStorage SoR는
 *    `orthus-theme`, `useTheme`이 `<html data-theme>`을 base 값으로 stamp.
 * 2. 프리셋 테마 — theme-tokens.ts 코드 상수 프리셋 갤러리(스와치 + 탭 적용).
 * 3. 사용자 정의 테마 — localStorage `orthus-custom-themes` CRUD + 인라인 색
 *    에디터(base 토글, 그룹별 color input, 고급 disclosure, 디바운스 라이브
 *    프리뷰, 저장/취소, 토큰별 기본값 reset).
 *
 * 디자인:
 * - Ojosama 토큰만 사용(라이트/다크 양쪽에서 동일한 CSS variable이 flip).
 * - 390×844 (P5 primary)에서 horizontal scroll 없음, primary control 44px+.
 * - 알려진 제한: 개인 보드(personal-ojosama.css)는 로컬 팔레트라 사용자 정의
 *   테마가 닿지 않는다 — UI에 안내 문구로 노출.
 */

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  BASE_TOKEN_VALUES,
  isDerivationSourceOverridden,
  PRESET_THEMES,
  THEME_TOKENS,
  TOKEN_GROUPS,
  type ThemeBase,
  type ThemeDefinition,
  type ThemeTokenDef,
} from "@/lib/theme-tokens";
import { useCustomThemes } from "@/lib/use-custom-themes";
import {
  applyThemeResolution,
  CUSTOM_THEME_PREFIX,
  effectiveTokenValue,
  reapplyStoredTheme,
  useTheme,
  type ThemePreference,
} from "@/lib/use-theme";

/** True after first client render. Used to avoid SSR/CSR mismatch on the
 *  resolved-theme readout (the value differs between server and the inline
 *  FOUC script's resolution). useSyncExternalStore returns the server snapshot
 *  on server, then the client snapshot post-hydration — no set-state-in-effect.
 */
function subscribeMount(): () => void { return () => undefined; }
function getMountedClient() { return true; }
function getMountedServer() { return false; }

interface ThemeOption {
  value: ThemePreference;
  label: string;
  description: string;
  icon: React.ReactNode;
}

const THEME_OPTIONS: ReadonlyArray<ThemeOption> = [
  {
    value: "light",
    label: "라이트",
    description: "밝은 화면. 작업 보드 기본 톤.",
    icon: <SunIcon />,
  },
  {
    value: "dark",
    label: "다크",
    description: "어두운 화면. 야간/저조도 작업에 적합.",
    icon: <MoonIcon />,
  },
  {
    value: "system",
    label: "시스템",
    description: "OS 설정을 따라간다.",
    icon: <DeviceIcon />,
  },
];

function newThemeId(): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    /* 구형 webview — fallback */
  }
  return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** #rgb → #rrggbb, #rrggbbaa → alpha 제거 — <input type="color">는 #rrggbb만 받는다. */
function normalizeHexForColorInput(value: string): string {
  const v = value.trim().toLowerCase();
  if (/^#[0-9a-f]{3}$/.test(v)) {
    return `#${v[1]}${v[1]}${v[2]}${v[2]}${v[3]}${v[3]}`;
  }
  if (/^#[0-9a-f]{8}$/.test(v)) return v.slice(0, 7);
  if (/^#[0-9a-f]{6}$/.test(v)) return v;
  return "#000000";
}

/** 텍스트 입력 커밋용: '#' 유무 허용, 유효하면 #rrggbb로 정규화, 아니면 null. */
function parseHexText(raw: string): string | null {
  const v = raw.trim().toLowerCase().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/.test(v) || /^[0-9a-f]{6}$/.test(v)) {
    return normalizeHexForColorInput(`#${v}`);
  }
  return null;
}

/**
 * base 기본값과 같은 override는 저장 전에 제거(토큰별 reset과 자연 일치).
 * 단, 파생 source(TOKEN_DERIVATIONS, 전이 포함)가 함께 override된 토큰은
 * 겉보기 기본값 pin이어도 제거하면 source 값으로 resolve가 흘러가 저장본이
 * 프리뷰와 달라진다 — 그대로 보존한다(예: --channel-blue를 갈고
 * --color-accent를 기본 hex로 고정한 경우).
 */
function pruneDefaultOverrides(base: ThemeBase, overrides: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [token, value] of Object.entries(overrides)) {
    if (
      !isDerivationSourceOverridden(token, overrides) &&
      BASE_TOKEN_VALUES[base][token]?.toLowerCase() === value.toLowerCase()
    ) {
      continue;
    }
    out[token] = value;
  }
  return out;
}

export default function AppearanceSettingsPage() {
  const { theme, setTheme, resolvedTheme, activeTheme } = useTheme();
  const { themes: customThemes, saveTheme, deleteTheme } = useCustomThemes();

  // Render the resolved-theme readout only after mount so SSR markup matches
  // the first client paint. The inline FOUC script has already applied the
  // correct `data-theme` to <html> at this point.
  const mounted = useSyncExternalStore(subscribeMount, getMountedClient, getMountedServer);

  const [draft, setDraft] = useState<ThemeDefinition | null>(null);

  // 라이브 프리뷰 — draft 변경을 디바운스해 문서에 직접 적용. 취소/unmount 시
  // reapplyStoredTheme()이 저장된 선호로 되돌린다(멱등).
  const previewTimer = useRef<number | null>(null);
  useEffect(() => {
    if (!draft) return;
    previewTimer.current = window.setTimeout(() => {
      applyThemeResolution({ base: draft.base, overrides: draft.overrides, activeTheme: null });
    }, 150);
    return () => {
      if (previewTimer.current != null) {
        window.clearTimeout(previewTimer.current);
        previewTimer.current = null;
      }
    };
  }, [draft]);

  // 취소/저장은 effect cleanup(비동기)보다 먼저 실행된다 — 방금 armed된
  // 프리뷰 타이머가 뒤늦게 발화해 버려진 draft를 전체 화면에 재적용하지
  // 않도록 두 경로 진입부에서 동기적으로 해제한다.
  function clearPreviewTimer() {
    if (previewTimer.current != null) {
      window.clearTimeout(previewTimer.current);
      previewTimer.current = null;
    }
  }

  useEffect(() => () => reapplyStoredTheme(), []);

  function startNewDraft() {
    setDraft({
      id: newThemeId(),
      name: "내 테마",
      base: resolvedTheme,
      // 현재 프리셋/사용자 정의 테마에서 시작 — 빌트인이면 빈 overrides.
      overrides: { ...(activeTheme?.overrides ?? {}) },
    });
  }

  function duplicateTheme(src: ThemeDefinition) {
    setDraft({
      id: newThemeId(),
      name: `${src.name} 복사`,
      base: src.base,
      overrides: { ...src.overrides },
    });
  }

  function cancelDraft() {
    clearPreviewTimer();
    setDraft(null);
    reapplyStoredTheme();
  }

  function saveDraft() {
    if (!draft) return;
    clearPreviewTimer();
    const clean: ThemeDefinition = {
      ...draft,
      name: draft.name.trim() || "이름 없는 테마",
      overrides: pruneDefaultOverrides(draft.base, draft.overrides),
    };
    saveTheme(clean);
    setTheme(`${CUSTOM_THEME_PREFIX}${clean.id}`);
    setDraft(null);
  }

  function removeTheme(target: ThemeDefinition) {
    if (typeof window !== "undefined" && !window.confirm(`"${target.name}" 테마를 삭제할까요?`)) {
      return;
    }
    deleteTheme(target.id);
    if (draft?.id === target.id) setDraft(null);
    if (theme === `${CUSTOM_THEME_PREFIX}${target.id}`) {
      // 삭제된 테마가 적용 중이면 그 base 모드로 안착(화면 톤 유지).
      setTheme(target.base);
    } else {
      reapplyStoredTheme();
    }
  }

  const activeLabel = !mounted
    ? "…"
    : activeTheme
      ? `${activeTheme.name} (${resolvedTheme === "dark" ? "다크" : "라이트"} 기반)`
      : resolvedTheme === "dark"
        ? "다크"
        : "라이트";

  return (
    <div
      className="h-full w-full overflow-y-auto"
      style={{ background: "var(--color-app-canvas)" }}
    >
      <div className="mx-auto flex max-w-[640px] flex-col gap-4 px-4 py-6 pb-[calc(24px+var(--mobile-safe-bottom))] sm:px-6 sm:py-8">
        <header className="flex flex-col gap-1.5">
          <h1
            className="text-[var(--text-day-heading)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
          >
            테마 설정
          </h1>
          <p
            className="text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            대리.ai 전체 화면에 적용된다. 기본 라이트·다크 외에 프리셋을 고르거나
            색을 직접 조합한 나만의 테마를 만들 수 있다.
          </p>
        </header>

        <section
          aria-label="기본 테마"
          className="flex flex-col gap-4 rounded-[var(--radius-modal)] p-4 sm:p-5"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-card)",
          }}
        >
          <div
            className="flex flex-wrap items-baseline gap-2"
            style={{ color: "var(--color-text-strong)" }}
          >
            <h2 className="text-[var(--text-body)] font-bold">기본 테마</h2>
            <span
              aria-live="polite"
              className="font-[family-name:var(--font-mono)] text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {`현재 적용: ${activeLabel}`}
            </span>
          </div>

          <fieldset
            aria-label="기본 테마 선택"
            className="m-0 grid gap-2 border-0 p-0 sm:grid-cols-3"
          >
            <legend className="sr-only">기본 테마 선택</legend>
            {THEME_OPTIONS.map((option) => {
              const active = theme === option.value;
              return (
                <label
                  className="flex cursor-pointer flex-col items-stretch gap-1 rounded-[var(--radius-toolbar)] p-3 transition-colors"
                  key={option.value}
                  style={{
                    background: active
                      ? "var(--color-sidebar-active)"
                      : "var(--color-panel)",
                    border: active
                      ? "1.5px solid var(--color-text-strong)"
                      : "1px solid var(--color-toolbar-border)",
                    minHeight: 64,
                  }}
                >
                  <input
                    aria-label={option.label}
                    checked={active}
                    className="sr-only"
                    name="theme"
                    onChange={() => setTheme(option.value)}
                    type="radio"
                    value={option.value}
                  />
                  <div className="flex items-center gap-2">
                    <span
                      aria-hidden
                      className="flex h-5 w-5 shrink-0 items-center justify-center"
                      style={{
                        color: active
                          ? "var(--color-text-strong)"
                          : "var(--color-text-muted)",
                      }}
                    >
                      {option.icon}
                    </span>
                    <span
                      className="text-[var(--text-body)] font-bold"
                      style={{
                        color: active
                          ? "var(--color-text-strong)"
                          : "var(--color-text-body)",
                      }}
                    >
                      {option.label}
                    </span>
                    {active ? (
                      <span
                        aria-hidden
                        className="ml-auto flex h-4 w-4 items-center justify-center"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        <CheckIcon />
                      </span>
                    ) : null}
                  </div>
                  <div
                    className="text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {option.description}
                  </div>
                </label>
              );
            })}
          </fieldset>
        </section>

        <section
          aria-label="프리셋 테마"
          className="flex flex-col gap-4 rounded-[var(--radius-modal)] p-4 sm:p-5"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-card)",
          }}
        >
          <div className="flex flex-col gap-0.5">
            <h2
              className="text-[var(--text-body)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              프리셋 테마
            </h2>
            <p
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              탭 한 번으로 적용. 프리셋에서 시작해 나만의 테마로 만들 수도 있다.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {PRESET_THEMES.map((preset) => {
              const active = theme === preset.id;
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => setTheme(preset.id)}
                  className="flex min-h-[64px] cursor-pointer flex-col items-stretch gap-2 rounded-[var(--radius-toolbar)] p-3 text-left transition-colors"
                  style={{
                    background: active
                      ? "var(--color-sidebar-active)"
                      : "var(--color-panel)",
                    border: active
                      ? "1.5px solid var(--color-text-strong)"
                      : "1px solid var(--color-toolbar-border)",
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="text-[var(--text-body)] font-bold"
                      style={{
                        color: active
                          ? "var(--color-text-strong)"
                          : "var(--color-text-body)",
                      }}
                    >
                      {preset.name}
                    </span>
                    <span
                      className="text-[var(--text-badge)]"
                      style={{ color: "var(--color-text-faint)" }}
                    >
                      {preset.base === "dark" ? "다크 기반" : "라이트 기반"}
                    </span>
                    {active ? (
                      <span
                        aria-hidden
                        className="ml-auto flex h-4 w-4 shrink-0 items-center justify-center"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        <CheckIcon />
                      </span>
                    ) : null}
                  </div>
                  <ThemeSwatches base={preset.base} overrides={preset.overrides} />
                </button>
              );
            })}
          </div>
        </section>

        <section
          aria-label="사용자 정의 테마"
          className="flex flex-col gap-4 rounded-[var(--radius-modal)] p-4 sm:p-5"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-card)",
          }}
        >
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex min-w-0 flex-col gap-0.5">
              <h2
                className="text-[var(--text-body)] font-bold"
                style={{ color: "var(--color-text-strong)" }}
              >
                사용자 정의 테마
              </h2>
              <p
                className="text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                라이트 또는 다크 팔레트 위에 원하는 색을 얹는다.
              </p>
            </div>
            <button
              type="button"
              onClick={startNewDraft}
              className="ml-auto inline-flex min-h-11 shrink-0 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-bold"
              style={{
                background: "var(--color-primary)",
                color: "var(--color-on-primary)",
              }}
            >
              + 새 테마 만들기
            </button>
          </div>

          {customThemes.length === 0 && !draft ? (
            <p
              className="rounded-[var(--radius-toolbar)] px-3 py-3 text-[var(--text-body-sm)]"
              style={{
                background: "var(--color-panel)",
                border: "1px dashed var(--color-toolbar-border)",
                color: "var(--color-text-muted)",
              }}
            >
              저장된 사용자 정의 테마가 없다. 현재 화면(또는 적용 중인 프리셋)에서
              시작해 새 테마를 만들어 보자.
            </p>
          ) : null}

          {customThemes.length > 0 ? (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {customThemes.map((custom) => {
                const active = theme === `${CUSTOM_THEME_PREFIX}${custom.id}`;
                return (
                  <li
                    key={custom.id}
                    className="flex flex-col gap-2 rounded-[var(--radius-toolbar)] p-3"
                    style={{
                      background: active
                        ? "var(--color-sidebar-active)"
                        : "var(--color-panel)",
                      border: active
                        ? "1.5px solid var(--color-text-strong)"
                        : "1px solid var(--color-toolbar-border)",
                    }}
                  >
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span
                        className="min-w-0 truncate text-[var(--text-body)] font-bold"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        {custom.name}
                      </span>
                      <span
                        className="shrink-0 text-[var(--text-badge)]"
                        style={{ color: "var(--color-text-faint)" }}
                      >
                        {custom.base === "dark" ? "다크 기반" : "라이트 기반"}
                      </span>
                      {active ? (
                        <span
                          className="shrink-0 rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)] font-bold"
                          style={{
                            background: "var(--color-pass-bg)",
                            color: "var(--color-pass-fg)",
                          }}
                        >
                          적용 중
                        </span>
                      ) : null}
                      <span className="ml-auto shrink-0">
                        <ThemeSwatches base={custom.base} overrides={custom.overrides} />
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <SmallActionButton
                        onClick={() => setTheme(`${CUSTOM_THEME_PREFIX}${custom.id}`)}
                        disabled={active}
                      >
                        적용
                      </SmallActionButton>
                      <SmallActionButton onClick={() => setDraft({ ...custom, overrides: { ...custom.overrides } })}>
                        편집·이름변경
                      </SmallActionButton>
                      <SmallActionButton onClick={() => duplicateTheme(custom)}>복제</SmallActionButton>
                      <SmallActionButton tone="danger" onClick={() => removeTheme(custom)}>
                        삭제
                      </SmallActionButton>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : null}

          {draft ? (
            <ThemeEditor
              draft={draft}
              isExisting={customThemes.some((t) => t.id === draft.id)}
              onChange={setDraft}
              onSave={saveDraft}
              onCancel={cancelDraft}
            />
          ) : null}

          <p
            className="text-[var(--text-meta)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            개인 보드(/board)는 아직 사용자 정의 색이 적용되지 않고 기본
            라이트·다크로 표시된다. 저장 위치: 이 브라우저(localStorage{" "}
            <code
              className="font-[family-name:var(--font-mono)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              orthus-theme
            </code>
            ,{" "}
            <code
              className="font-[family-name:var(--font-mono)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              orthus-custom-themes
            </code>
            ). 디바이스마다 따로 설정하며 로그아웃해도 유지된다.
          </p>
        </section>
      </div>
    </div>
  );
}

/* ------------------------- custom theme editor --------------------------- */

function ThemeEditor({
  draft,
  isExisting,
  onChange,
  onSave,
  onCancel,
}: {
  draft: ThemeDefinition;
  isExisting: boolean;
  onChange: (next: ThemeDefinition) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const [showAdvanced, setShowAdvanced] = useState(false);

  const setToken = (token: string, value: string) => {
    onChange({ ...draft, overrides: { ...draft.overrides, [token]: value } });
  };
  const resetToken = (token: string) => {
    const next = { ...draft.overrides };
    delete next[token];
    onChange({ ...draft, overrides: next });
  };

  const coreGroups = TOKEN_GROUPS.map((group) => ({
    group,
    tokens: THEME_TOKENS.filter((t) => t.group === group.id && t.core),
  })).filter((g) => g.tokens.length > 0);
  const advancedGroups = TOKEN_GROUPS.map((group) => ({
    group,
    tokens: THEME_TOKENS.filter((t) => t.group === group.id && !t.core),
  })).filter((g) => g.tokens.length > 0);

  return (
    <div
      className="flex flex-col gap-4 rounded-[var(--radius-toolbar)] p-3 sm:p-4"
      style={{
        background: "var(--color-panel)",
        border: "1px solid var(--color-toolbar-border)",
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3
          className="text-[var(--text-body)] font-bold"
          style={{ color: "var(--color-text-strong)" }}
        >
          {isExisting ? "테마 편집" : "새 테마"}
        </h3>
        <span
          className="text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          변경하면 화면에 바로 미리 적용된다. 취소하면 원래 테마로 돌아간다.
        </span>
      </div>

      <label className="flex flex-col gap-1">
        <span
          className="text-[var(--text-meta)] font-semibold"
          style={{ color: "var(--color-text-muted)" }}
        >
          테마 이름
        </span>
        <input
          type="text"
          value={draft.name}
          maxLength={40}
          onChange={(e) => onChange({ ...draft, name: e.target.value })}
          className="min-h-11 rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body)]"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            color: "var(--color-text-strong)",
          }}
        />
      </label>

      <fieldset className="m-0 flex flex-col gap-1 border-0 p-0">
        <legend
          className="mb-1 p-0 text-[var(--text-meta)] font-semibold"
          style={{ color: "var(--color-text-muted)" }}
        >
          기반 팔레트
        </legend>
        <div className="grid grid-cols-2 gap-2">
          {(["light", "dark"] as const).map((base) => {
            const active = draft.base === base;
            return (
              <button
                key={base}
                type="button"
                onClick={() => onChange({ ...draft, base })}
                className="inline-flex min-h-11 items-center justify-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-bold"
                style={{
                  background: active
                    ? "var(--color-sidebar-active)"
                    : "var(--color-card)",
                  border: active
                    ? "1.5px solid var(--color-text-strong)"
                    : "1px solid var(--color-toolbar-border)",
                  color: active
                    ? "var(--color-text-strong)"
                    : "var(--color-text-muted)",
                }}
              >
                {base === "light" ? "라이트 기반" : "다크 기반"}
              </button>
            );
          })}
        </div>
        <p
          className="text-[var(--text-meta)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          지정하지 않은 색은 기반 팔레트 기본값을 그대로 쓴다.
        </p>
      </fieldset>

      {coreGroups.map(({ group, tokens }) => (
        <TokenGroupEditor
          key={group.id}
          label={group.label}
          tokens={tokens}
          draft={draft}
          onSet={setToken}
          onReset={resetToken}
        />
      ))}

      <div
        className="flex flex-col gap-3 border-t pt-3"
        style={{ borderColor: "var(--color-divider-soft)" }}
      >
        <button
          type="button"
          onClick={() => setShowAdvanced((v) => !v)}
          aria-expanded={showAdvanced}
          className="inline-flex min-h-11 items-center gap-1.5 self-start rounded-[var(--radius-toolbar)] px-2 text-[var(--text-body-sm)] font-semibold"
          style={{ color: "var(--color-text-muted)" }}
        >
          <span
            aria-hidden
            className="inline-block transition-transform"
            style={{ transform: showAdvanced ? "rotate(90deg)" : "none" }}
          >
            ▸
          </span>
          고급 토큰 {showAdvanced ? "접기" : "펼치기"}
        </button>
        {showAdvanced
          ? advancedGroups.map(({ group, tokens }) => (
              <TokenGroupEditor
                key={group.id}
                label={group.label}
                tokens={tokens}
                draft={draft}
                onSet={setToken}
                onReset={resetToken}
              />
            ))
          : null}
      </div>

      <div
        className="flex flex-wrap items-center justify-end gap-2 border-t pt-3"
        style={{ borderColor: "var(--color-divider-soft)" }}
      >
        <button
          type="button"
          onClick={onCancel}
          className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-4 text-[var(--text-body-sm)] font-semibold"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            color: "var(--color-text-body)",
          }}
        >
          취소
        </button>
        <button
          type="button"
          onClick={onSave}
          className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-4 text-[var(--text-body-sm)] font-bold"
          style={{
            background: "var(--color-primary)",
            color: "var(--color-on-primary)",
          }}
        >
          저장하고 적용
        </button>
      </div>
    </div>
  );
}

function TokenGroupEditor({
  label,
  tokens,
  draft,
  onSet,
  onReset,
}: {
  label: string;
  tokens: readonly ThemeTokenDef[];
  draft: ThemeDefinition;
  onSet: (token: string, value: string) => void;
  onReset: (token: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <h4
        className="text-[var(--text-meta)] font-semibold"
        style={{ color: "var(--color-text-muted)" }}
      >
        {label}
      </h4>
      <div className="grid gap-1.5 sm:grid-cols-2">
        {tokens.map((def) => (
          <TokenRow
            key={def.token}
            def={def}
            value={effectiveTokenValue(draft.base, draft.overrides, def.token)}
            overridden={def.token in draft.overrides}
            onSet={onSet}
            onReset={onReset}
          />
        ))}
      </div>
    </div>
  );
}

function TokenRow({
  def,
  value,
  overridden,
  onSet,
  onReset,
}: {
  def: ThemeTokenDef;
  value: string;
  overridden: boolean;
  onSet: (token: string, value: string) => void;
  onReset: (token: string) => void;
}) {
  const colorValue = normalizeHexForColorInput(value);

  const commitText = (input: HTMLInputElement) => {
    const parsed = parseHexText(input.value);
    if (parsed && parsed !== colorValue) {
      onSet(def.token, parsed);
    } else {
      // 무효 입력은 현재 유효값으로 되돌린다(uncontrolled — key remount 불필요).
      input.value = value;
    }
  };

  return (
    <div
      className="flex flex-wrap items-center gap-2 rounded-[var(--radius-toolbar)] px-2 py-1"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-divider-soft)",
      }}
    >
      <span
        className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-body)" }}
        title={def.token}
      >
        {def.label}
        {overridden ? (
          <span aria-hidden style={{ color: "var(--color-accent)" }}>
            {" "}
            ●
          </span>
        ) : null}
      </span>
      <input
        aria-label={`${def.label} 색상`}
        type="color"
        value={colorValue}
        onChange={(e) => onSet(def.token, e.target.value)}
        className="h-11 w-12 shrink-0 cursor-pointer rounded-[4px] border-0 bg-transparent p-0"
      />
      <input
        aria-label={`${def.label} hex 값`}
        key={`${def.token}:${value}`}
        defaultValue={value}
        spellCheck={false}
        inputMode="text"
        onBlur={(e) => commitText(e.currentTarget)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commitText(e.currentTarget);
        }}
        className="min-h-11 w-[92px] shrink-0 rounded-[4px] px-2 font-[family-name:var(--font-mono)] text-[var(--text-body-sm)]"
        style={{
          background: "var(--color-panel)",
          border: "1px solid var(--color-toolbar-border)",
          color: "var(--color-text-body)",
        }}
      />
      <button
        type="button"
        onClick={() => onReset(def.token)}
        disabled={!overridden}
        title="기반 팔레트 기본값으로 되돌리기"
        className="inline-flex min-h-11 shrink-0 items-center rounded-[4px] px-2 text-[var(--text-meta)] font-semibold disabled:opacity-35"
        style={{ color: "var(--color-text-muted)" }}
      >
        기본값
      </button>
    </div>
  );
}

/* ------------------------------ pieces ----------------------------------- */

function SmallActionButton({
  children,
  onClick,
  disabled,
  tone,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: "danger";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold disabled:opacity-45"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        color: tone === "danger" ? "var(--color-fail-fg)" : "var(--color-text-body)",
      }}
    >
      {children}
    </button>
  );
}

/** 테마 미리보기 스와치 — 캔버스/카드/사이드바/강조/텍스트 강조 5색. */
function ThemeSwatches({
  base,
  overrides,
}: {
  base: ThemeBase;
  overrides: Record<string, string>;
}) {
  const tokens = [
    "--color-app-canvas",
    "--color-card",
    "--color-sidebar",
    "--color-accent",
    "--color-text-strong",
  ];
  return (
    <span aria-hidden className="flex items-center gap-1">
      {tokens.map((token) => (
        <span
          key={token}
          className="h-4 w-4 rounded-full"
          style={{
            background: effectiveTokenValue(base, overrides, token),
            border: "1px solid var(--color-divider)",
          }}
        />
      ))}
    </span>
  );
}

/* ----------------------------- icons ------------------------------------- */

function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3v2" />
      <path d="M12 19v2" />
      <path d="M3 12h2" />
      <path d="M19 12h2" />
      <path d="m5.6 5.6 1.5 1.5" />
      <path d="m16.9 16.9 1.5 1.5" />
      <path d="m5.6 18.4 1.5-1.5" />
      <path d="m16.9 7.1 1.5-1.5" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
    </svg>
  );
}

function DeviceIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8" />
      <path d="M12 16v4" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m5 12 5 5L20 7" />
    </svg>
  );
}
