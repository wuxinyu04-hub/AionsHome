// PWA 静态资源缓存：/static/** 与 /public/** 走 stale-while-revalidate，
// HTML 文档不拦截（保留后端 no-cache 语义）。资源更新靠 ?v= 版本串失效。
// 旧哄睡离线缓存（已下线 2026-08-01）在 activate 时清理。
const CACHE_PREFIX = 'aion-static';
const CACHE_NAME = CACHE_PREFIX + '-20260810';  // 整体失效时 bump 版本号

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(Promise.all([
    self.clients.claim(),
    caches.keys().then(keys =>
      Promise.all(keys.map(k => k === CACHE_NAME ? null : caches.delete(k))),
    ),
  ]));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const p = url.pathname;
  if (!p.startsWith('/static/') && !p.startsWith('/public/')) return;
  e.respondWith(staleWhileRevalidate(req));
});

async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(req);
  const network = fetch(req).then(res => {
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  }).catch(() => cached);
  return cached || network;
}
