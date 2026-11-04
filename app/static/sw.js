// Offline support for the phone: pages and game data network-first (fall back to the last copy);
// versioned scripts and styles (?v=<hash>), fonts and item/hero images cache-first.
const CACHE = "deadlock-v2";
const IMAGES = "https://assets-bucket.deadlock-api.com/";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()),
));

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const resp = await fetch(req);
    if (resp.ok) cache.put(req, resp.clone());
    return resp;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(req);
  if (hit) return hit;
  const resp = await fetch(req);
  if (resp.ok || resp.type === "opaque") cache.put(req, resp.clone());
  return resp;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.href.startsWith(IMAGES) || url.hostname.endsWith("fonts.gstatic.com") || url.hostname === "fonts.googleapis.com") {
    return e.respondWith(cacheFirst(req));
  }
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) return e.respondWith(url.search ? cacheFirst(req) : networkFirst(req));
  // Only the game-data pages are worth keeping offline; match pages change all the time.
  if (url.pathname === "/gamedata.json" || /^\/(items|heroes|planner|builds|patches)(\/|$)/.test(url.pathname)) {
    return e.respondWith(networkFirst(req));
  }
});
