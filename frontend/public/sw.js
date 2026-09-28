/* JARVIS service worker — hand-written, intentionally minimal.
 * Caches ONLY static build assets (app shell, hashed JS/CSS, icons).
 * Never caches /api responses, auth, or anything that could contain secrets. */
const CACHE = 'jarvis-static-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith('/assets/') || SHELL.includes(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Never touch API traffic: always network, never cached.
  if (url.pathname.startsWith('/api/') || url.pathname === '/api') return;

  if (req.mode === 'navigate') {
    // Network first for the HTML shell; fall back to the cached shell when offline.
    event.respondWith(
      fetch(req).catch(() => caches.match('/').then((r) => r || new Response('JARVIS is offline.', { status: 503 }))),
    );
    return;
  }
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            if (res.ok && res.type === 'basic') {
              const copy = res.clone();
              caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return res;
          }),
      ),
    );
  }
});

// Web Push: show minimal lock-screen content; details stay in the app.
self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: event.data ? event.data.text() : 'JARVIS' };
  }
  const title = data.title || 'JARVIS';
  const opts = { body: data.body || '', tag: data.id || undefined, data: { id: data.id || null } };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const id = event.notification.data && event.notification.data.id;
  const target = id ? '/notifications?opened=' + encodeURIComponent(id) : '/notifications';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) {
          w.navigate(target);
          return w.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
