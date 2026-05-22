// Minimal service worker — just enough to make the PWA installable.
// We don't aggressively cache anything: the audio backend is dynamic.

const CACHE = 'jarvis-9e3ae1f';
const SHELL = ['/', '/index.html', '/styles.css?v=9e3ae1f', '/app.js?v=9e3ae1f', '/manifest.json?v=9e3ae1f'];

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
