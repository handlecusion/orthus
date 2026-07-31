import type { Metadata, Viewport } from "next";
import { Inter, Sometype_Mono } from "next/font/google";
import { AppShell } from "@/components/AppShell";
import { PRESET_THEMES, THEME_TOKENS, TOKEN_ALIASES } from "@/lib/theme-tokens";
import "./globals.css";

/*
 * Single product font (Inter) — Ojosama uses one face across the whole UI.
 * Sometype Mono stays for query/SQL/identifier slots only.
 */

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  display: "swap",
});

const sometypeMono = Sometype_Mono({
  variable: "--font-sometype-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "대리.ai — 아카식 + 비서",
  description:
    "사내 지식을 기록하고(아카식), 자연어로 안전하게 질의하는(비서) 워크스페이스.",
  icons: {
    icon: [
      { url: "/favicon.ico?v=20260615-greenwork", sizes: "any" },
      { url: "/icon-32.png?v=20260615-greenwork", sizes: "32x32", type: "image/png" },
      { url: "/icon-192.png?v=20260615-greenwork", sizes: "192x192", type: "image/png" },
    ],
    apple: [{ url: "/apple-icon.png?v=20260615-greenwork", sizes: "180x180", type: "image/png" }],
  },
};

/*
 * Mobile viewport: device-width scaling + `viewport-fit=cover` so notch/home-bar
 * safe-area insets (env(safe-area-inset-*)) resolve to real values on phones.
 * User zoom is intentionally left enabled (no maximumScale) for accessibility.
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0b0b0c" },
  ],
};

/*
 * FOUC 스크립트에 inline되는 상수 테이블 — theme-tokens.ts(SoR)에서 module
 * scope에 생성해 프리셋/alias가 코드와 항상 동기화된다(layout은 server
 * component라 상수 import는 안전; 스크립트 문자열 자체는 import 없이 동작).
 */
const THEME_PRESET_TABLE = JSON.stringify(
  Object.fromEntries(PRESET_THEMES.map((p) => [p.id, { base: p.base, overrides: p.overrides }])),
);
const THEME_ALIAS_TABLE = JSON.stringify(TOKEN_ALIASES);
/** allowlist 토큰 → 1. 런타임 sanitizeOverrides와 동일한 필터를 FOUC에서도 적용. */
const THEME_TOKEN_ALLOWLIST = JSON.stringify(
  Object.fromEntries(THEME_TOKENS.map((t) => [t.token, 1])),
);

/**
 * Inline FOUC guard. Runs synchronously before first paint to resolve the
 * user's theme preference (light|dark|system|preset id|custom:<id>), stamp
 * `data-theme` (base mode) on <html>, and setProperty preset/custom token
 * overrides (+ Tailwind alias twins) so the first frame already carries the
 * right palette. Unknown/deleted ids resolve like "system"; any throw falls
 * back to plain light. Custom entries mirror the runtime parser: invalid base
 * is dropped and only allowlist tokens with hex values are applied
 * (sanitizeOverrides invariant — no arbitrary CSS strings or non-allowlist
 * properties on the FOUC path; applyThemeResolution's cleanup sweep only
 * removes allowlist tokens, so writing anything else here would leak stale
 * inline styles across theme switches). A corrupt custom-themes blob is
 * caught by its own inner try (parseThemes semantics — treated as empty) so it
 * degrades to the base/system fallback below instead of the outer light-only
 * catch. No deps, synchronous, runs once.
 */
const THEME_INIT_SCRIPT = `
(function(){
  try {
    var d = document.documentElement;
    var presets = ${THEME_PRESET_TABLE};
    var aliases = ${THEME_ALIAS_TABLE};
    var allow = ${THEME_TOKEN_ALLOWLIST};
    var hex = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;
    var p = localStorage.getItem("orthus-theme") || "system";
    var base = null, ov = null;
    if (p === "light" || p === "dark") { base = p; }
    else if (presets[p]) { base = presets[p].base; ov = presets[p].overrides; }
    else if (p.indexOf("custom:") === 0) {
      var list = [];
      try { list = JSON.parse(localStorage.getItem("orthus-custom-themes") || "[]"); } catch (pe) { list = []; }
      if (!Array.isArray(list)) list = [];
      for (var i = 0; i < list.length; i++) {
        var t = list[i];
        if (t && t.id === p.slice(7) && (t.base === "light" || t.base === "dark")) {
          base = t.base;
          ov = t.overrides;
          break;
        }
      }
    }
    if (base !== "light" && base !== "dark") {
      base = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    d.dataset.theme = base;
    if (ov && typeof ov === "object") {
      for (var k in ov) {
        if (allow[k] === 1 && typeof ov[k] === "string" && hex.test(ov[k])) {
          d.style.setProperty(k, ov[k]);
          if (aliases[k]) d.style.setProperty(aliases[k], ov[k]);
        }
      }
    }
  } catch (e) {
    document.documentElement.dataset.theme = "light";
  }
})();
`.trim();

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="ko"
      className={`${inter.variable} ${sometypeMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="h-full">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
