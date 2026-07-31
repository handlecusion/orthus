"use client";

import { useEffect } from "react";

// Public browsers remain off by default and require the operator flag. When the
// flag is off, prior registrations/caches are actively removed as the
// public-site kill switch (docs/offline-personal-board.md 3단계).
const SW_ENABLED = process.env.NEXT_PUBLIC_OFFLINE_SW_ENABLED === "true";

export function OfflineServiceWorker() {
  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    // The public browser surface keeps the operator kill-switch.
    const enabled = SW_ENABLED;

    if (!enabled) {
      // Kill switch: remove any SW this browser previously installed.
      void navigator.serviceWorker.getRegistrations().then((regs) => {
        for (const reg of regs) {
          reg.active?.postMessage("orthus-sw-unregister");
          void reg.unregister();
        }
      });
      return;
    }

    const onLoad = () => {
      void navigator.serviceWorker
        .register("/sw.js")
        .then(() => navigator.serviceWorker.ready)
        .then((registration) => {
          if (["/board", "/notes", "/notes/quick"].includes(window.location.pathname)) {
            registration.active?.postMessage({
              type: "orthus-cache-shell",
              path: window.location.pathname,
            });
          }
        })
        .catch(() => {
          /* registration failure is non-fatal — the app still works online */
        });
    };
    if (document.readyState === "complete") onLoad();
    else window.addEventListener("load", onLoad, { once: true });
    return () => window.removeEventListener("load", onLoad);
  }, []);

  return null;
}
