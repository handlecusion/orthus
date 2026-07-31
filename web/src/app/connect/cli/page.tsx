"use client";

/* CLI 셀프 온보딩 브라우저 연결 페이지 (P10.8).
 *
 * `orthus connect`가 로컬 루프백 서버를 띄운 뒤 이 페이지를 연다:
 *   {central}/connect/cli?state={state}&port={port}&name={hostname}
 * 로그인된 사용자가 [이 기기 연결]을 누르면 전체 권장 스코프의 collector
 * 토큰이 발급되고(항상 본인 계정 바인딩 — 서버 `session_member` + self-bound),
 * OAuth 스타일 루프백 redirect로 CLI에 전달된다:
 *   http://127.0.0.1:{port}/callback?state={state}&token={dct_...}
 *
 * 보안 계약: redirect host는 127.0.0.1 하드코드(호스트/redirect 파라미터를
 * 절대 받지 않음), port는 1024..65535 정수만, state는 불투명 문자열
 * 최대 128자, 발급은 반드시 명시적 클릭(로드 시 자동 발급 없음). */

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { Banner, ToolbarButton } from "@/components/ui";

// /connectors "연결 준비" 패널의 토큰과 동일한 전체 권장 스코프.
// knowledge:write 포함 — 봇/CLI의 내 보드 티켓 CRUD(ticket_add 등)가 이 토큰으로
// 동작해야 한다 (owner-bound라 자기 보드만 쓰기 가능; 2026-07-19 e2e 403 실측 보정).
const CLI_TOKEN_SCOPES = ["ingest", "commands", "knowledge", "knowledge:write", "agent_task"];

const MAX_STATE_LENGTH = 128;

function parseLoopbackPort(raw: string | null): number | null {
  if (!raw || !/^\d{4,5}$/.test(raw)) return null;
  const port = Number(raw);
  return Number.isInteger(port) && port >= 1024 && port <= 65535 ? port : null;
}

const cardStyle = {
  background: "var(--color-card)",
  border: "1px solid var(--color-divider)",
} as const;

function ConnectCliInner() {
  const searchParams = useSearchParams();
  const state = searchParams.get("state");
  const port = parseLoopbackPort(searchParams.get("port"));
  const nameParam = searchParams.get("name");
  const stateValid = !!state && state.length <= MAX_STATE_LENGTH;
  const paramsValid = stateValid && port !== null;

  const [label, setLabel] = useState(() => nameParam?.trim() || "내 Mac");
  const [phase, setPhase] = useState<"idle" | "issuing" | "redirected" | "cancelled">(
    "idle",
  );
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function connectThisDevice() {
    // 발급은 명시적 클릭에서만 — 자동 발급 없음. 파라미터가 유효하지 않으면
    // 버튼 자체가 렌더되지 않지만, 방어적으로 한 번 더 확인한다.
    if (!paramsValid || !state || port === null) return;
    setError(null);
    setPhase("issuing");
    try {
      const deviceLabel = label.trim() || "내 Mac";
      const issued = await api.issueCollectorToken({
        name: deviceLabel,
        scopes: [...CLI_TOKEN_SCOPES],
        device_label: deviceLabel,
      });
      setIssuedToken(issued.token);
      setPhase("redirected");
      // 루프백 host는 127.0.0.1 하드코드 — 페이지는 host/redirect 파라미터를
      // 받지 않는다. CLI 계약: /callback?state=...&token=...
      window.location.href =
        `http://127.0.0.1:${port}/callback?state=${encodeURIComponent(state)}` +
        `&token=${encodeURIComponent(issued.token)}`;
    } catch (e) {
      setPhase("idle");
      setError(e instanceof Error ? e.message : "토큰 발급에 실패했습니다.");
    }
  }

  async function copyToken() {
    if (!issuedToken) return;
    try {
      await navigator.clipboard.writeText(issuedToken);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard 차단 — 사용자가 직접 드래그 복사할 수 있게 토큰은 그대로 노출.
    }
  }

  return (
    <div className="h-full min-h-0 w-full overflow-y-auto p-0 min-[760px]:p-3">
      <div className="mx-auto w-full max-w-[560px] px-3 py-4 min-[760px]:px-0">
        <header className="mb-3">
          <h1
            className="text-[17px] font-bold tracking-[-0.02em]"
            style={{ color: "var(--color-text-strong)" }}
          >
            CLI 연결
          </h1>
          <p
            className="mt-0.5 text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            이 기기의 orthus CLI가 회사 계정으로 연결을 요청했습니다.
          </p>
        </header>

        {!paramsValid ? (
          <Banner tone="fail" title="연결 요청이 올바르지 않습니다">
            {!stateValid
              ? "state 파라미터가 없거나 형식이 잘못됐습니다."
              : "port 파라미터가 없거나 허용 범위(1024–65535)가 아닙니다."}{" "}
            터미널에서 <code>orthus connect</code>를 다시 실행해 주세요.
          </Banner>
        ) : phase === "cancelled" ? (
          <div
            className="rounded-[10px] px-4 py-8 text-center text-[var(--text-body-sm)]"
            style={{ ...cardStyle, color: "var(--color-text-muted)" }}
          >
            연결을 취소했습니다. 브라우저 창을 닫으세요.
          </div>
        ) : (
          <div className="grid gap-3">
            <section className="rounded-[10px] px-4 py-3" style={cardStyle}>
              <h2
                className="text-[var(--text-body-sm)] font-bold"
                style={{ color: "var(--color-text-strong)" }}
              >
                발급되는 토큰 권한
              </h2>
              <p
                className="mt-0.5 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                전체 권장 스코프 — 커넥터 화면의 &ldquo;연결 준비&rdquo; 토큰과
                동일하며, 항상 본인 계정에만 바인딩됩니다.
              </p>
              <ul className="mt-2 flex flex-wrap gap-1.5">
                {CLI_TOKEN_SCOPES.map((scope) => (
                  <li
                    key={scope}
                    className="inline-flex items-center rounded-[999px] px-2 py-0.5 font-mono text-[var(--text-meta)] font-semibold"
                    style={{
                      background:
                        "color-mix(in srgb, var(--color-accent) 14%, var(--color-card))",
                      color: "var(--color-text-body)",
                    }}
                  >
                    {scope}
                  </li>
                ))}
              </ul>
            </section>

            <section className="rounded-[10px] px-4 py-3" style={cardStyle}>
              <label
                htmlFor="device-label"
                className="block text-[var(--text-body-sm)] font-bold"
                style={{ color: "var(--color-text-strong)" }}
              >
                기기 이름
              </label>
              <input
                id="device-label"
                type="text"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                disabled={phase !== "idle"}
                className="mt-1.5 h-11 w-full rounded-[8px] px-3 text-[var(--text-body-sm)] min-[760px]:h-9"
                style={{
                  background: "var(--color-bg)",
                  border: "1px solid var(--color-divider)",
                  color: "var(--color-text-body)",
                }}
              />
            </section>

            {error ? (
              <Banner tone="fail" title="토큰 발급에 실패했습니다">
                {error}
              </Banner>
            ) : null}

            {phase !== "redirected" ? (
              <div className="flex flex-wrap items-center gap-2">
                <ToolbarButton
                  tone="accent"
                  className="min-h-[44px] px-5"
                  disabled={phase === "issuing"}
                  onClick={() => void connectThisDevice()}
                >
                  {phase === "issuing" ? "연결 중…" : "이 기기 연결"}
                </ToolbarButton>
                <ToolbarButton
                  className="min-h-[44px] px-4"
                  disabled={phase === "issuing"}
                  onClick={() => setPhase("cancelled")}
                >
                  취소
                </ToolbarButton>
              </div>
            ) : (
              <div className="grid gap-2">
                <Banner tone="pass" title="토큰을 발급했습니다">
                  CLI로 돌아가는 중입니다. 터미널을 확인하세요.
                </Banner>
                <details className="rounded-[10px] px-4 py-3" style={cardStyle}>
                  <summary
                    className="cursor-pointer text-[var(--text-body-sm)] font-bold"
                    style={{ color: "var(--color-text-body)" }}
                  >
                    CLI로 돌아가지 못했다면
                  </summary>
                  <p
                    className="mt-2 text-[var(--text-body-sm)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    아래 토큰을 복사해 <code>orthus init --mcp-token-stdin</code>
                    으로 등록하세요.
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <code
                      className="min-w-0 flex-1 break-all rounded-[8px] px-3 py-2 font-mono text-[var(--text-meta)]"
                      style={{
                        background: "var(--color-bg)",
                        border: "1px solid var(--color-divider)",
                        color: "var(--color-text-body)",
                      }}
                    >
                      {issuedToken}
                    </code>
                    <ToolbarButton
                      tone="primary"
                      className="min-h-[44px]"
                      onClick={() => void copyToken()}
                    >
                      {copied ? "복사됨" : "토큰 복사"}
                    </ToolbarButton>
                  </div>
                </details>
              </div>
            )}

            <p
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              본인이 방금 터미널에서 실행한 <code>orthus connect</code> 요청일
              때만 눌러야 합니다. 직접 실행하지 않았다면 이 창을 닫으세요.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

export default function ConnectCliPage() {
  return (
    <Suspense fallback={null}>
      <ConnectCliInner />
    </Suspense>
  );
}
