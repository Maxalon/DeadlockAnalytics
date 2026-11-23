// Offline support for the phone: pages and game data network-first (fall back to the last copy);
// versioned scripts and styles (?v=<hash>), fonts and item/hero images cache-first.
// Images are replaced under the same address when the game updates, so they have a cache of
// their own that starts afresh whenever the game data changes.
const CACHE = "deadlock-v3";
const IMAGE_CACHE = "deadlock-images";
const GAME_DATA_MARK = "/__game-data-version";
const IMAGES = "https://assets-bucket.deadlock-api.com/";
const FONT_HOSTS = new Set(["fonts.gstatic.com", "fonts.googleapis.com"]);

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== IMAGE_CACHE).map((k) => caches.delete(k))))
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

/** The game data, and with a new version of it (its ETag) the item and hero images again. */
async function gameData(req) {
  const resp = await networkFirst(req);
  const version = resp.headers.get("etag");
  if (resp.ok && version) {
    const cache = await caches.open(CACHE);
    const seen = await cache.match(GAME_DATA_MARK).then((r) => r?.text());
    if (seen !== version) {
      await caches.delete(IMAGE_CACHE);
      await cache.put(GAME_DATA_MARK, new Response(version));
    }
  }
  return resp;
}

async function cacheFirst(req, name = CACHE) {
  const cache = await caches.open(name);
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
  if (url.href.startsWith(IMAGES)) return e.respondWith(cacheFirst(req, IMAGE_CACHE));
  if (FONT_HOSTS.has(url.hostname)) return e.respondWith(cacheFirst(req));
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) return e.respondWith(url.search ? cacheFirst(req) : networkFirst(req));
  // Only the game-data pages are worth keeping offline; match pages change all the time.
  if (url.pathname === "/gamedata.json") return e.respondWith(gameData(req));
  if (/^\/(items|heroes|planner|builds|patches)(\/|$)/.test(url.pathname)) return e.respondWith(networkFirst(req));
});
