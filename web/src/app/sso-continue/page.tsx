"use client";

import { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

/**
 * Hub-side resume hop for cross-node SSO.
 *
 * When a relying-party node sends an unauthenticated user to the hub to log in,
 * the hub login redirects here after success. This page forwards to the hub's
 * /auth/sso/issue endpoint (now with a hub session) so a ticket is minted and
 * the browser is bounced back to the originating node. The issue endpoint
 * re-validates return_to against its trusted-origin allowlist, so a tampered
 * query cannot redirect the ticket anywhere untrusted.
 */
export default function SsoContinuePage() {
  return (
    <Suspense fallback={null}>
      <SsoContinue />
    </Suspense>
  );
}

function SsoContinue() {
  const params = useSearchParams();

  useEffect(() => {
    if (typeof window === "undefined") return;
    const returnTo = params.get("return_to") || "";
    const next = params.get("next") || "/editor";
    if (!returnTo) {
      window.location.assign("/login");
      return;
    }
    window.location.assign(api.ssoIssueUrl(returnTo, next));
  }, [params]);

  return null;
}
