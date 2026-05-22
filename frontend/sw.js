// Minimal service worker — just enough to make the PWA installable.
// We don't aggressively cache anything: the audio backend is dynamic.

const CACHE = 'jarvis-f3b7971';
const SHELL = ['/', '/index.html', '/styles.css?v=f3b7971', '/app.js?v=f3b7971', '/manifest.json?v=f3b7971'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Never cache API calls
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/chat/api/')) return;
  // For the shell, network-first then cache
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
