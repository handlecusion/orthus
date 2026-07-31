"use client";

import { useEffect, useState } from "react";

export const COMPACT_AGENT_WORK_WIDTH = 760;

/**
 * Shared compact-shell breakpoint for the Agent (chat) and 체크리스트 (queue)
 * pages. Both must use the same threshold so P5 mobile parity stays consistent.
 */
export function useCompactAgentWorkShell() {
  const [compactShell, setCompactShell] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    function syncCompactShell() {
      setCompactShell(window.innerWidth < COMPACT_AGENT_WORK_WIDTH);
    }
    syncCompactShell();
    window.addEventListener("resize", syncCompactShell);
    return () => {
      window.removeEventListener("resize", syncCompactShell);
    };
  }, []);

  return compactShell;
}
