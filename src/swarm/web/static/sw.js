const CACHE_NAME = 'swarm-v5';
const APP_SHELL = ['/', '/manifest.json', '/static/bees/happy.svg', '/static/icon-192.png', '/static/icon-512.png', '/offline.html'];

const INLINE_OFFLINE = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swarm — Offline</title>
<style>body{background:#2A1B0E;color:#E8DCC8;font-family:monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
h1{color:#D8A03D;font-size:1.4rem}p{color:#7A6B5A;font-size:.9rem;margin:.5rem 0}</style></head>
<body><div><h1>Waiting for Swarm...</h1><p>The server should restart automatically.</p><p>Retrying...</p>
<script>setInterval(function(){fetch('/api/health').then(function(r){if(r.ok)location.replace('/')}).catch(function(){})},3000)</script>
</div></body></html>`;

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
    // Race fetch against a 2s timeout to avoid blank page flash
    e.respondWith(
      Promise.race([
        fetch(req).then(resp => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
          return resp;
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 2000))
      ]).catch(() =>
        caches.match('/offline.html').then(cached =>
          cached || new Response(INLINE_OFFLINE, {
            status: 503,
            headers: { 'Content-Type': 'text/html' }
          })
        )
      )
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
