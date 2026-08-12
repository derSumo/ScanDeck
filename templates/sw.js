/* Scan Deck service worker: app shell offline, API always live.
   Diese Datei wird von Flask ausgeliefert, damit die Version eingesetzt werden
   kann. Der Cache-Name enthaelt sie: jede neue Version bekommt einen eigenen
   Cache, alte werden beim Aktivieren geloescht. Ohne das wuerde eine
   installierte App das CSS und JS der ersten Installation behalten. */
const VERSION = "{{ version }}";
const SHELL_CACHE = `scandeck-shell-${VERSION}`;
const SHELL_ASSETS = [
  "/",
  `/static/app.css?v=${VERSION}`,
  `/static/app.js?v=${VERSION}`,
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

/* Push: erreicht die installierte App auch, wenn sie geschlossen ist. */
self.addEventListener("push", (event) => {
  let payload = { title: "ScanDeck", body: "Scan fertig.", tag: "scandeck" };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    /* Text statt JSON: dann bleibt die Vorgabe stehen */
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      tag: payload.tag,
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      vibrate: [80, 40, 80],
      renotify: true,
    })
  );
});

/* Tippen auf die Meldung bringt die App nach vorn, statt sie neu zu oeffnen. */
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      const open = clients.find((client) => "focus" in client);
      if (open) return open.focus();
      return self.clients.openWindow("/");
    })
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Live data must never be answered from a cache.
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put("/", copy));
          return response;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // Stale-while-revalidate: aus dem Cache antworten, aber im Hintergrund
  // erneuern. Damit landet eine Aenderung spaetestens beim naechsten Start,
  // auch wenn die Versionsnummer einmal gleich bleibt.
  event.respondWith(
    caches.open(SHELL_CACHE).then((cache) =>
      cache.match(request).then((cached) => {
        const network = fetch(request)
          .then((response) => {
            if (response.ok) cache.put(request, response.clone());
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    )
  );
});
