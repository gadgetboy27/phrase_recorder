// PhraseKit service worker: keeps the app shell available so the icon opens instantly, even before the
// Nano answers. The shell is network-first (an update is picked up on the next open, never served stale
// for days); API calls are never cached — every /phrases, /panel/*, /audio/* goes to the Nano.
const SHELL = "phrasekit-shell-v1";
const FILES = ["/", "/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !FILES.includes(url.pathname)) return;       // API: straight through
  e.respondWith(
    fetch(e.request).then((r) => { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); return r; })
      .catch(() => caches.match(e.request))
  );
});
