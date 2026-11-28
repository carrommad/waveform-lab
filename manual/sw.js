const CACHE_NAME = "synth-guide-v1";
const ASSETS = [
  "./",
  "./index.html",
  "./menus.json",
  "./manifest.json",
  "../favicon.svg",
  "./pdfs/Manual_Instrucciones_Akai_MPK_Mini.pdf",
  "./pdfs/Manual_Instrucciones_Akai_APC40_MKII.pdf",
  "./pdfs/Manual_Instrucciones_Magic_Trackpad.pdf"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((resp) => {
          if (resp && resp.status === 200 && resp.type === "basic") {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(event.request, clone));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
