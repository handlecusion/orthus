"use client";

import { type FormEvent, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Building2Icon, MailIcon } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, type AuthConfig } from "@/lib/api";
import { BrandMark } from "@/components/BrandMark";
import { Banner, Card, ToolbarButton, inputClass, inputStyle } from "@/components/ui";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginContent />
    </Suspense>
  );
}

function LoginContent() {
  const router = useRouter();
  const params = useSearchParams();
  // same-origin 경로만 허용 (백엔드 safe_next_path와 동일 규칙 — open redirect 방지)
  const next = useMemo(() => {
    // 로그인 후 기본 도착지 = 내 보드(2026-07-08 오너 요청, 루트 `/`와 동일).
    const raw = params.get("next") || "/board?view=personal";
    return raw.startsWith("/") && !raw.startsWith("//") && !raw.includes("://")
      ? raw
      : "/board?view=personal";
  }, [params]);
  const error = params.get("error");
  const ssoParam = params.get("sso");
  const devParam = params.get("dev");
  const devAutoTriedRef = useRef(false);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const emailDomain = config?.email_domain || "acme.example";
  const [localPart, setLocalPart] = useState("");
  const [devEmail, setDevEmail] = useState("");
  const [message, setMessage] = useState<{ tone: "pass" | "warn" | "fail"; title: string; body?: string } | null>(null);
  const [pending, setPending] = useState(false);
  // 세션 자동 통과 확인(/auth/me)의 비동기 결과. "no-session"이 되어야 폼을 그리고,
  // 성공 시에는 폼 없이 로딩 화면을 유지한 채 바로 앱으로 이동한다 — 이미 로그인된
  // 사용자가 접속할 때 로그인 화면이 번쩍 보였다 사라지는 것을 없앤다.
  const [sessionProbe, setSessionProbe] = useState<"pending" | "no-session">("pending");

  useEffect(() => {
    let alive = true;
    api
      .getAuthConfig()
      .then((data) => {
        if (alive) setConfig(data);
      })
      .catch((err) => {
        if (alive) setMessage({ tone: "fail", title: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      alive = false;
    };
  }, []);

  // 이미 유효한 세션 쿠키가 있으면(브라우저가 /login 탭을 복원한 경우 등) 로그인
  // 폼을 보여주지 않고 바로 앱으로 보낸다. 401/403이면 그때 폼을 그린다.
  // ?error=... 로 돌아온 경우는 사유를 보여줘야 하므로 자동 이동하지 않는다.
  useEffect(() => {
    if (!config || config.auth_mode !== "session" || error) return;
    let alive = true;
    // /auth/me가 응답을 물고 있는 극단 상황에서 로딩 화면에 갇히지 않게 하는 안전판.
    const fallback = window.setTimeout(() => {
      if (alive) setSessionProbe("no-session");
    }, 6000);
    api
      .getAuthMe()
      .then(() => {
        if (!alive) return;
        window.clearTimeout(fallback);
        window.location.replace(next);
      })
      .catch(() => {
        if (!alive) return;
        window.clearTimeout(fallback);
        setSessionProbe("no-session");
      });
    return () => {
      alive = false;
      window.clearTimeout(fallback);
    };
  }, [config, error, next]);

  // RP nodes: try a silent cross-node SSO handoff once before showing the form.
  // A failed/no-session bounce returns with ?sso=... or ?error=... — the guard
  // (sessionStorage + those params) prevents a redirect loop.
  useEffect(() => {
    if (!config || config.sso_role !== "rp") return;
    if (error || ssoParam) return;
    if (typeof window === "undefined") return;
    const KEY = "orthus_sso_silent_tried";
    if (window.sessionStorage.getItem(KEY)) return;
    window.sessionStorage.setItem(KEY, "1");
    window.location.assign(api.ssoStartUrl(next, true));
  }, [config, error, ssoParam, next]);

  // 로컬 dev 편의: `?dev=<email>`이 있으면 dev-login을 자동 제출하고 next로 보낸다.
  // dev_login_enabled가 꺼진 프로덕션/S1에서는 아무 일도 하지 않는다(서버도 dev-login 404).
  useEffect(() => {
    if (!config || devAutoTriedRef.current) return;
    const canDev = config.auth_mode === "session" && config.dev_login_enabled;
    if (!canDev || !devParam) return;
    devAutoTriedRef.current = true;
    api
      .devLogin(devParam)
      .then(() => router.push(next))
      .catch((err) =>
        setMessage({ tone: "fail", title: err instanceof Error ? err.message : String(err) }),
      );
  }, [config, devParam, next, router]);

  function startSso() {
    window.location.assign(api.ssoStartUrl(next, false));
  }

  async function requestMagicLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    const value = localPart.trim();
    if (!value) {
      setMessage({ tone: "fail", title: "이메일을 입력하세요." });
      return;
    }
    setPending(true);
    try {
      const result = await api.requestMagicLink(value, next);
      if (result.delivery === "none") {
        setMessage({
          tone: "warn",
          title: "로그인 링크 요청 완료",
          body: "메일 발송 설정이 아직 연결되지 않았습니다.",
        });
      } else {
        setMessage({
          tone: "pass",
          title: "메일을 확인하세요.",
          body: "허용된 이메일이면 로그인 링크가 발송됩니다.",
        });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setMessage({ tone: "fail", title: `@${emailDomain} 앞부분만 입력하세요.` });
      } else if (err instanceof ApiError) {
        setMessage({ tone: "fail", title: err.message });
      } else {
        setMessage({ tone: "fail", title: err instanceof Error ? err.message : String(err) });
      }
    } finally {
      setPending(false);
    }
  }

  async function devLogin() {
    setMessage(null);
    try {
      await api.devLogin(devEmail);
      router.push(next);
    } catch (err) {
      if (err instanceof ApiError) setMessage({ tone: "fail", title: err.message });
      else setMessage({ tone: "fail", title: err instanceof Error ? err.message : String(err) });
    }
  }

  const magicLinkEnabled = config?.auth_mode === "session";
  const devEnabled = config?.auth_mode === "session" && config.dev_login_enabled;
  const ssoEnabled = config?.sso_role === "rp";

  // 세션 확인/자동 이동이 끝나기 전(그리고 보여줄 에러도 없을 때)에는 폼 대신
  // 로고만 보여준다. 에러 배너/메시지가 있거나 세션 모드가 아니면 폼 카드를 그린다.
  // config가 아직 없을 때도 로딩을 유지해 "폼 → 사라짐" 번쩍임을 막는다.
  const probeActive = config == null || config.auth_mode === "session";
  if (probeActive && sessionProbe === "pending" && !error && !message) {
    return (
      <main
        className="flex min-h-screen items-center justify-center px-4 py-8"
        style={{ background: "var(--color-app-canvas)" }}
      >
        <div className="flex flex-col items-center gap-3">
          <BrandMark size="lg" />
          <span
            className="text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
            aria-live="polite"
          >
            세션 확인 중…
          </span>
        </div>
      </main>
    );
  }

  return (
    <main
      className="flex min-h-screen items-center justify-center px-4 py-8"
      style={{ background: "var(--color-app-canvas)" }}
    >
      <Card className="w-full max-w-[420px] p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1
              className="text-[var(--text-day-heading)] font-extrabold"
              style={{ color: "var(--color-text-strong)", lineHeight: 1.12 }}
            >
              로그인
            </h1>
            <p
              className="mt-1 text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {config ? `${config.node_id} · ${config.auth_mode}` : "auth"}
            </p>
          </div>
          <BrandMark size="lg" />
        </div>

        {error ? (
          <div className="mt-3">
            <Banner tone="fail" title={errorLabel(error)}>
              {errorDescription(error)}
            </Banner>
          </div>
        ) : null}
        {message ? (
          <div className="mt-3">
            <Banner tone={message.tone} title={message.title}>
              {message.body}
            </Banner>
          </div>
        ) : null}

        {ssoEnabled ? (
          <div className="mt-4 flex flex-col gap-3">
            <ToolbarButton
              type="button"
              tone="primary"
              icon={<Building2Icon size={16} strokeWidth={2.2} />}
              onClick={startSso}
              className="!h-11 justify-center"
            >
              회사 계정으로 로그인
            </ToolbarButton>
            <div
              className="flex items-center gap-2 text-[var(--text-meta)] font-bold"
              style={{ color: "var(--color-text-muted)" }}
            >
              <span className="h-px flex-1" style={{ background: "var(--color-divider)" }} />
              또는 회사 이메일로 로그인
              <span className="h-px flex-1" style={{ background: "var(--color-divider)" }} />
            </div>
          </div>
        ) : null}

        <form className="mt-4 flex flex-col gap-3" onSubmit={(event) => void requestMagicLink(event)}>
          <label className="text-[var(--text-meta)] font-bold" style={{ color: "var(--color-text-muted)" }}>
            회사 이메일
          </label>
          <div
            className="flex min-h-11 overflow-hidden rounded-[6px]"
            style={{
              background: "var(--color-panel)",
              border: "1px solid var(--color-divider)",
            }}
          >
            <input
              className={`${inputClass} min-h-11 min-w-0 flex-1 border-0`}
              style={{ ...inputStyle, boxShadow: "none" }}
              value={localPart}
              onChange={(e) => setLocalPart(e.target.value)}
              placeholder="name"
              autoComplete="username"
              inputMode="email"
              disabled={!magicLinkEnabled || pending}
            />
            <span
              className="flex min-h-11 shrink-0 items-center px-3 text-[var(--text-body-sm)] font-semibold"
              style={{
                color: "var(--color-text-muted)",
                borderLeft: "1px solid var(--color-divider-soft)",
              }}
            >
              @{emailDomain}
            </span>
          </div>
          <ToolbarButton
            type="submit"
            tone="primary"
            icon={<MailIcon size={16} strokeWidth={2.2} />}
            disabled={!magicLinkEnabled || pending}
            className="!h-11 justify-center"
          >
            {pending ? "요청 중" : "로그인 링크 받기"}
          </ToolbarButton>

          {devEnabled ? (
            <div
              className="mt-2 rounded-[6px] p-2"
              style={{
                background: "var(--color-panel)",
                border: "1px solid var(--color-divider-soft)",
              }}
            >
              <div className="flex gap-2">
                <input
                  className={inputClass}
                  style={inputStyle}
                  value={devEmail}
                  onChange={(e) => setDevEmail(e.target.value)}
                  placeholder="owner@example.com"
                  autoComplete="email"
                />
                <ToolbarButton type="button" onClick={() => void devLogin()}>
                  Dev
                </ToolbarButton>
              </div>
            </div>
          ) : null}
        </form>

        {!magicLinkEnabled ? (
          <p
            className="mt-3 text-[var(--text-body-sm)] leading-[1.5]"
            style={{ color: "var(--color-text-muted)" }}
          >
            세션 인증 설정 대기 중
          </p>
        ) : null}
      </Card>
    </main>
  );
}

function errorLabel(value: string): string {
  if (value === "not_invited") return "로그인할 수 없음";
  if (value === "email_not_verified") return "이메일 인증 필요";
  if (value === "magic_link_invalid") return "로그인 링크 만료";
  if (value === "oauth_invalid_state") return "로그인 세션 만료";
  if (value === "oauth_expired") return "로그인 만료";
  if (value === "oauth_exchange_failed") return "인증 실패";
  if (value === "oauth_invalid_response") return "인증 응답 검증 실패";
  if (value === "oauth_failed") return "로그인 실패";
  if (value === "sso_failed") return "회사 계정 로그인 실패";
  if (value === "logged_out") return "로그아웃됨";
  return "로그인 실패";
}

function errorDescription(value: string): string {
  if (value === "not_invited") {
    return "허용된 이메일인지 확인하고 다시 요청하세요.";
  }
  if (value === "email_not_verified") {
    return "이메일 인증 후 다시 시도하세요.";
  }
  if (value === "magic_link_invalid") {
    return "로그인 링크를 다시 요청하세요.";
  }
  if (value === "oauth_invalid_state") {
    return "로그인을 처음부터 다시 시작하세요.";
  }
  if (value === "oauth_expired") {
    return "로그인을 다시 시작하세요.";
  }
  if (value === "oauth_exchange_failed") {
    return "로그인을 다시 시작하세요.";
  }
  if (value === "oauth_invalid_response") {
    return "관리자에게 문의하거나 다시 시도하세요.";
  }
  if (value === "sso_failed") {
    return "회사 계정으로 다시 로그인하거나 회사 이메일 로그인을 사용하세요.";
  }
  return "다시 로그인하세요.";
}
