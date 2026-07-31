/*
 * orthus offline app-shell service worker (오프라인 3단계).
 *
 * Goal: let the desktop app / browser open 내 보드 with no network (cold start),
 * complementing the IndexedDB data cache (1·2단계). It is OFF by default and only
 * registered when NEXT_PUBLIC_OFFLINE_SW_ENABLED=true (see OfflineServiceWorker).
 *
 * Strategy (deploy-safe — the public web redeploys many times a day, so a sticky
 * stale shell must never brick the site):
 *   - Navigations (HTML): /board, /notes, and /notes/quick are all NETWORK-FIRST
 *     with a last-good offline fallback. (Earlier /notes/quick was CACHE-FIRST for
 *     instant paint, but the desktop app registers this SW and the public web
 *     redeploys many times a day, so a cached quick-note shell would keep pointing
 *     at deleted /_next/static chunk hashes and render broken even while online.
 *     The native prewarmed quick-note window already covers cold-paint latency, so
 *     network-first is both correct and fast enough.)
 *   - Hashed static assets (/_next/static/*): CACHE-FIRST. They are
 *     content-hashed so a cached copy is always correct for its URL.
 *   - Everything else (incl. /api and cross-origin): pass through untouched.
 *   - Versioned cache + skipWaiting + clients.claim so a new SW activates at once
 *     and old caches are pruned. Bump CACHE_VERSION to invalidate.
 */

const CACHE_VERSION = "v3";
const SHELL_CACHE = `orthus-shell-${CACHE_VERSION}`;
const STATIC_CACHE = `orthus-static-${CACHE_VERSION}`;
const SHELL_URLS = new Set(["/board", "/notes", "/notes/quick"]);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== SHELL_CACHE && k !== STATIC_CACHE)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

// Kill switch: the page can tell the SW to remove itself + all caches.
self.addEventListener("message", (event) => {
  if (event.data === "orthus-sw-unregister") {
    event.waitUntil(
      caches
        .keys()
        .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => self.registration.unregister()),
    );
    return;
  }
  if (
    event.data &&
    event.data.type === "orthus-cache-shell" &&
    SHELL_URLS.has(event.data.path)
  ) {
    const path = event.data.path;
    event.waitUntil(
      fetch(path, { credentials: "include", cache: "no-store" })
        .then((response) => cacheShell(path, response))
        .catch(() => undefined),
    );
  }
});

async function cacheShell(path, response) {
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok || response.redirected || !contentType.includes("text/html")) return response;
  const copy = response.clone();
  try {
    const cache = await caches.open(SHELL_CACHE);
    await cache.put(path, copy);
  } catch {
    // Cache quota/permission must never turn a good network response into an error.
  }
  return response;
}

function isStaticAsset(url) {
  return url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/desktop/");
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  // Only handle same-origin GETs; never touch the API (cross-origin) or others.
  if (url.origin !== self.location.origin) return;

  // Navigations → network-first with cached-shell fallback when offline.
  if (request.mode === "navigate") {
    const path = url.pathname.endsWith("/") && url.pathname !== "/"
      ? url.pathname.slice(0, -1)
      : url.pathname;
    const isShellNavigation = SHELL_URLS.has(path);
    event.respondWith(
      fetch(request)
        .then((response) => {
          return isShellNavigation ? cacheShell(path, response) : response;
        })
        .catch(() => {
          if (isShellNavigation) {
            return caches.match(path).then((cached) => cached ?? caches.match(request));
          }
          return caches.match(request).then((cached) => cached ?? Response.error());
        }),
    );
    return;
  }

  // Hashed static assets → cache-first (content-addressed, safe).
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ??
          fetch(request).then(async (response) => {
            if (response.ok) {
              const copy = response.clone();
              try {
                const cache = await caches.open(STATIC_CACHE);
                await cache.put(request, copy);
              } catch {
                // Online asset still succeeds when local cache is unavailable.
              }
            }
            return response;
          }),
      ),
    );
  }
});
