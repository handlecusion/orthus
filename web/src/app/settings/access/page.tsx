"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AllowlistEntry,
  type AuthMe,
} from "@/lib/api";
import {
  Banner,
  Card,
  Chip,
  PageHeader,
  Toolbar,
  ToolbarButton,
  inputClass,
  inputStyle,
} from "@/components/ui";

const ROLES = ["member", "viewer", "admin", "owner"] as const;

export default function AccessPage() {
  const [me, setMe] = useState<AuthMe | null>(null);
  const [entries, setEntries] = useState<AllowlistEntry[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setError(null);
    setLoading(true);
    try {
      const [meData, list] = await Promise.all([
        api.getAuthMe(),
        api.listAllowlist(),
      ]);
      setMe(meData);
      setEntries(list);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let alive = true;
    Promise.all([api.getAuthMe(), api.listAllowlist()])
      .then(([meData, list]) => {
        if (!alive) return;
        setMe(meData);
        setEntries(list);
      })
      .catch((err) => {
        if (alive) setError(errorText(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function addEntry() {
    setError(null);
    try {
      await api.addAllowlistEntry(email, role);
      setEmail("");
      await refresh();
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function revokeEntry(targetEmail: string) {
    setError(null);
    try {
      await api.revokeAllowlistEntry(targetEmail);
      await refresh();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <PageHeader
        title="접근 관리"
        subtitle={me ? `${me.node_id} · ${me.role ?? me.auth_mode}` : "auth"}
        right={
          <Toolbar>
            <ToolbarButton type="button" onClick={() => void refresh()}>
              새로고침
            </ToolbarButton>
          </Toolbar>
        }
      />

      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="mx-auto grid max-w-[980px] gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          <Card className="p-3">
            <h2
              className="text-[var(--text-body)] font-extrabold"
              style={{ color: "var(--color-text-strong)" }}
            >
              초대
            </h2>
            <div className="mt-3 flex flex-col gap-2">
              <input
                className={inputClass}
                style={inputStyle}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@example.com"
                autoComplete="email"
              />
              <select
                className={inputClass}
                style={inputStyle}
                value={role}
                onChange={(e) => setRole(e.target.value)}
              >
                {ROLES.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
              <ToolbarButton type="button" tone="primary" onClick={() => void addEntry()}>
                추가
              </ToolbarButton>
            </div>
          </Card>

          <div className="flex min-w-0 flex-col gap-3">
            {error ? <Banner tone="fail" title={error} /> : null}
            {loading ? (
              <Card className="p-4 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
                loading
              </Card>
            ) : entries.length === 0 ? (
              <Card className="p-4 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
                empty
              </Card>
            ) : (
              entries.map((entry) => (
                <Card key={entry.allowlist_id} className="p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div
                        className="truncate text-[var(--text-body)] font-semibold"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        {entry.email}
                      </div>
                      <div
                        className="mt-1 flex flex-wrap items-center gap-2 text-[var(--text-meta)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        <Chip tone={entry.revoked_at ? "fail" : "pass"}>
                          {entry.revoked_at ? "revoked" : "active"}
                        </Chip>
                        <span className="font-[family-name:var(--font-mono)]">
                          {entry.role}
                        </span>
                      </div>
                    </div>
                    <ToolbarButton
                      type="button"
                      disabled={Boolean(entry.revoked_at)}
                      onClick={() => void revokeEntry(entry.email)}
                    >
                      해제
                    </ToolbarButton>
                  </div>
                </Card>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
