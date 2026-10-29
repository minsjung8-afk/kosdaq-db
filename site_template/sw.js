/* 심사참고 DB 서비스워커(PWA, 2026-10-29)
   원칙: 데이터는 항상 최신이 우선 — 화면(index)과 data/*.json은 네트워크 먼저, 실패(오프라인)할 때만 저장본을 보여줌.
   차트 라이브러리·아이콘처럼 거의 안 바뀌는 파일만 저장본 먼저. /api/(회사 추가·방문자 집계)는 건드리지 않음. */
const VER = '__BUILD__';
const SHELL = 'shell-' + VER, DATA = 'data-v1';
const PRE = ['./', 'index.html', 'manifest.webmanifest', 'lib/lightweight-charts.standalone.production.js',
             'icon-192.png', 'icon-512.png', 'data/companies.json'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(PRE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith('shell-') && k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
const netFirst = async (req, cacheName) => {
  const c = await caches.open(cacheName);
  try {
    const r = await fetch(req, {cache: 'no-store'});
    if (r && r.ok) c.put(req, r.clone());
    return r;
  } catch (err) {
    const hit = await c.match(req, {ignoreSearch: true}) || await caches.match(req, {ignoreSearch: true});
    if (hit) return hit;
    throw err;
  }
};
self.addEventListener('fetch', e => {
  const req = e.request, u = new URL(req.url);
  if (req.method !== 'GET' || u.origin !== location.origin || u.pathname.includes('/api/')) return;
  if (req.mode === 'navigate' || u.pathname.endsWith('/') || u.pathname.endsWith('.html')) {
    e.respondWith(netFirst(req, SHELL).catch(() => caches.match('index.html')));
  } else if (u.pathname.includes('/data/')) {
    e.respondWith(netFirst(req, DATA));          // 열어 본 회사는 오프라인에서도 마지막 저장본으로 열림
  } else if (/\/lib\//.test(u.pathname) || /\.(png|webmanifest)$/.test(u.pathname)) {
    e.respondWith(caches.match(req).then(h => h || fetch(req).then(r => { const cl = r.clone(); caches.open(SHELL).then(c => c.put(req, cl)); return r; })));
  }
});
