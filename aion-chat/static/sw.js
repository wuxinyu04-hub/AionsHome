// PWA 生命周期占位：哄睡离线已下线（2026-08-01），不再拦截任何请求，浏览器默认透传。
// 只做一件事：activate 时清掉旧版哄睡缓存（含 v4 声道错乱的拼接 mp3），避免残留坏音频。
self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(Promise.all([
    self.clients.claim(),
    caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))),
  ]));
});
