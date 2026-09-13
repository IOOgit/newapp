// Service worker: установка PWA + офлайн-заглушка.
// Стратегия "сначала сеть" — чтобы обновления стилей/страниц всегда подтягивались,
// а кэш использовался только когда нет связи.
const CACHE = "nvrmon-v12";

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // действия (POST/PUT) — всегда напрямую в сеть
  e.respondWith(
    fetch(req).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
      return resp;
    }).catch(() => caches.match(req).then((r) => r || caches.match("/offline")))
  );
});
