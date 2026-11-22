// Caches the app shell so it opens instantly. Budget data (/api/) is never cached.
const VERSION = "banktab-v8";
const SHELL = ["/", "/static/styles.css?v=6", "/static/app.js?v=6", "/static/theme.js?v=6", "/static/icon.svg?v=2", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Network first so updates show up right away; fall back to cache when offline.
  e.respondWith(
    fetch(e.request, { cache: "no-cache" })  // revalidate with the server, never trust the HTTP cache
      .then((r) => {
        if (r.ok) { const copy = r.clone(); caches.open(VERSION).then((c) => c.put(e.request, copy)); }
        return r;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});
