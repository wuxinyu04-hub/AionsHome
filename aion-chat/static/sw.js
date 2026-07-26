// PWA lifecycle + 透传 + 哄睡离线缓存。
// 音频（/api/sleep/*/audio）走 cache-first：合成后的 mp3 不可变，命中缓存直接回，
// 未命中才走网络并落缓存——配合页面端"离线保存/睡前预载"显式下载（sleep.js cacheItem）。
// 其他哄睡 GET（/sleep 页面、/static/sleep.*、/api/sleep/* 非音频）走 stale-while-revalidate：
// 有缓存立即返回 + 后台更新；无缓存等网络；离线返回缓存。
// 其他请求不响应（浏览器默认透传），不影响 aion-chat 其他页面。
// SLEEP_CACHE 名称须与 sleep.js 里的 CACHE 一致。
// v4：v3 时期缓存过声道错乱的拼接 mp3（播到边界卡死），换名整体作废重新下载
const SLEEP_CACHE = 'aion-sleep-v4';

self.addEventListener('install', e => {
  self.skipWaiting();
  // 预缓存哄睡首页（HTML）；sleep.js?v= 和音频靠首次访问/显式下载落缓存
  e.waitUntil(caches.open(SLEEP_CACHE).then(c => c.addAll(['/sleep']).catch(() => {})));
});

self.addEventListener('activate', e => {
  e.waitUntil(Promise.all([
    self.clients.claim(),
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== SLEEP_CACHE).map(k => caches.delete(k)))),
  ]));
});

self.addEventListener('fetch', e => {
  const path = new URL(e.request.url).pathname;
  if (e.request.method !== 'GET') return;
  const isSleep = path === '/sleep' || path.startsWith('/static/sleep') || path.startsWith('/api/sleep/');
  if (!isSleep) return; // 非哄睡请求：不 respondWith，浏览器默认透传

  // 音频与白噪音素材都不可变 -> cache-first
  const isAudio = /^\/api\/sleep\/([^/]+\/audio|noise\/[^/]+)$/.test(path);

  e.respondWith((async () => {
    const cache = await caches.open(SLEEP_CACHE);
    // 音频：cache-first（mp3 合成后不可变；ignoreSearch/Range 由浏览器对整响应自行处理）
    const cached = await cache.match(e.request, { ignoreVary: true });
    if (isAudio && cached) return cached;

    const networkPromise = fetch(e.request).then(resp => {
      if (resp.ok) cache.put(e.request, resp.clone()).catch(() => {});
      return resp;
    }).catch(() => null);

    if (cached) {
      e.waitUntil(networkPromise); // 非音频：stale-while-revalidate，后台更新
      return cached;
    }
    return (await networkPromise) || new Response('离线且无缓存', { status: 504, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  })());
});
