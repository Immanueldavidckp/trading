/* trade.scratchforge — service worker.
 *
 * Deliberately conservative: market data must never be served stale, so every
 * /api/ and /ws/ request bypasses the cache entirely. Only the app shell and
 * third-party static assets are cached, which is what makes the installed app
 * open instantly instead of waiting on the network for its own HTML.
 */
const V = 'sf-v1';
const SHELL = ['/m.html', '/icons/icon-192.png', '/icons/icon-512.png', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  // live data + auth: always straight to the network, never cached
  if (url.origin === location.origin &&
      (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws'))) return;

  // navigations: network first so a deploy is picked up immediately,
  // cache only as the offline fallback
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then(r => { const c = r.clone(); caches.open(V).then(x => x.put('/m.html', c)); return r; })
        .catch(() => caches.match('/m.html'))
    );
    return;
  }

  // static assets (icons, fonts, echarts): cache first
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (r.ok && (url.origin === location.origin || url.hostname.includes('jsdelivr') || url.hostname.includes('gstatic'))) {
        const c = r.clone(); caches.open(V).then(x => x.put(req, c));
      }
      return r;
    }).catch(() => hit))
  );
});
