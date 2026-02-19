const CACHE_NAME = 'swarm-v4';
const APP_SHELL = ['/', '/manifest.json', '/static/bees/happy.svg', '/static/icon-192.png', '/static/icon-512.png', '/offline.html'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Never cache WebSocket upgrades or API/action calls
  if (url.pathname.startsWith('/action/') || url.pathname.startsWith('/ws')) return;

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(resp => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(req, clone));
        return resp;
      }).catch(() => caches.match('/offline.html'))
    );
    return;
  }

  e.respondWith(
    fetch(req).then(resp => {
      const clone = resp.clone();
      caches.open(CACHE_NAME).then(c => c.put(req, clone));
      return resp;
    }).catch(() => caches.match(req))
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
      for (const client of clients) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow('/');
    })
  );
});
