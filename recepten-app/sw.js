// Offline-laag voor de geïnstalleerde app (PWA).
// De app zelf komt uit de cache zodat hij ook zonder bereik opent (handig in de winkel);
// de folders worden altijd eerst vers opgehaald.
const CACHE = 'aanbiedingskeuken-v17';
// Alleen deze externe bronnen mogen uit de cache komen (bibliotheken en lettertypen, die veranderen niet).
// Al het andere van buiten, zoals de tijdlijn van Supabase, gaat altijd rechtstreeks naar het netwerk.
const CDN = ['cdnjs.cloudflare.com', 'cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com'];
const SHELL = ['./', './index.html', './manifest.webmanifest', './icons/icon-192.png', './icons/icon-512.png',
  'https://cdnjs.cloudflare.com/ajax/libs/jsbarcode/3.11.6/JsBarcode.all.min.js'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin && !CDN.includes(url.hostname)) return;
  // Folders en de app-pagina: eerst het netwerk (nieuwste versie), anders de cache
  if (url.pathname.endsWith('aanbiedingen.json') || req.mode === 'navigate') {
    e.respondWith(fetch(req).then(res => {
      const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); return res;
    }).catch(() => caches.match(req).then(r => r || caches.match('./index.html'))));
    return;
  }
  // Iconen, lettertypen en bibliotheken: eerst de cache
  e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
    if (res.ok || res.type === 'opaque') { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  })));
});
