'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Service } = require('../src/service');
const { GcError } = require('../src/gc');
const config = require('../src/config');

const fixture = fs.readFileSync(path.join(__dirname, 'fixtures', '115701682.meta.bz2'));

function service({ salts, download }) {
  const calls = { salts: 0, urls: [], targets: [] };
  const gc = {
    actions: [],
    friends: () => ({ logged_in: true, incoming: [], friends: [], blocked: [] }),
    async friendAction(id, action) {
      if (action === 'nope') throw new GcError('bad_request', 'unknown action');
      this.actions.push([id, action]);
    },
    declineAll: () => 3,
    status: () => ({ gc_ready: true }),
    matchSalts: async (id, target) => { calls.salts += 1; calls.targets.push(target); return salts(id); },
    fullHistory: async () => ({ matches: [{ match_id: '115701682', hero_id: 3 }], complete: true }),
    profiles: async (ids) => ids.map((id) => ({ account_id: id, name: `p${id}`, avatar_url: null })),
  };
  const fetchFn = async (url) => { calls.urls.push(url); return download(url); };
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'fetcher-'));
  return { svc: new Service({ gc, cacheDir, replayUrl: config.replayUrl, fetch: fetchFn }), calls, gc };
}

test('match: salts -> download from the replay cluster -> decoded, then cached', async () => {
  const { svc, calls } = service({
    salts: () => ({ cluster_id: 3, metadata_salt: 1234 }),
    download: () => new Response(fixture),
  });
  const first = await svc.handle('/matches/115701682');
  assert.strictEqual(first.status, 200);
  assert.strictEqual(first.body.players.length, 12);
  assert.deepStrictEqual(calls.urls, ['http://replay3.valve.net/1422450/115701682_1234.meta.bz2']);
  const second = await svc.handle('/matches/115701682');
  assert.deepStrictEqual(second.body, first.body);
  assert.strictEqual(calls.salts, 1); // served from the cache
});

test('a match cached by an older decoder is decoded again from its kept file, without salts', async () => {
  const { svc, calls } = service({
    salts: () => ({ cluster_id: 3, metadata_salt: 1234 }),
    download: () => new Response(fixture),
  });
  await svc.handle('/matches/115701682');
  const cached = path.join(svc.cacheDir, '115701682.json');
  const { badge, ...old } = JSON.parse(fs.readFileSync(cached, 'utf8')).players[0];
  fs.writeFileSync(cached, JSON.stringify({ match_id: 115701682, players: [old] })); // no `decoder`: version 1
  const again = await svc.handle('/matches/115701682');
  assert.strictEqual(again.body.players.length, 12);
  assert.ok('badge' in again.body.players[0]);
  assert.strictEqual(calls.salts, 1);
  assert.strictEqual(JSON.parse(fs.readFileSync(cached, 'utf8')).decoder, again.body.decoder);
});

test('profiles: 1 to 100 SteamID3s', async () => {
  const { svc } = service({ salts: () => null, download: () => null });
  const res = await svc.handle('/profiles', 'GET', new URLSearchParams({ ids: '1,2,x' }));
  assert.deepStrictEqual(res.body.profiles.map((p) => p.name), ['p1', 'p2']);
  assert.strictEqual((await svc.handle('/profiles')).status, 400);
  const many = Array.from({ length: 101 }, (_, i) => i + 1).join(',');
  assert.strictEqual((await svc.handle('/profiles', 'GET', new URLSearchParams({ ids: many }))).status, 400);
});

test('match files Valve no longer has, and GC errors, become HTTP statuses', async () => {
  const { svc } = service({ salts: () => ({ cluster_id: 3, metadata_salt: 1 }), download: () => new Response('', { status: 404 }) });
  assert.strictEqual((await svc.handle('/matches/5')).status, 404);
  const { svc: limited } = service({ salts: () => { throw new GcError('rate_limited'); }, download: () => null });
  assert.strictEqual((await limited.handle('/matches/5')).status, 429);
});

test('history and routing', async () => {
  const { svc } = service({ salts: () => null, download: () => null });
  const res = await svc.handle('/history/113956776');
  assert.strictEqual(res.status, 200);
  assert.deepStrictEqual(res.body.matches, [{ match_id: 115701682, hero_id: 3 }]);
  assert.strictEqual((await svc.handle('/nope')).status, 404);
  assert.strictEqual((await svc.handle('/history/abc')).status, 404);
});

test('friends routes', async () => {
  const { svc, gc } = service({ salts: () => null, download: () => null });
  assert.strictEqual((await svc.handle('/friends')).status, 200);
  const res = await svc.handle('/friends/42/accept', 'POST');
  assert.strictEqual(res.status, 200);
  assert.deepStrictEqual(gc.actions, [[42, 'accept']]);
  assert.deepStrictEqual((await svc.handle('/friends/decline-all', 'POST')).body, { declined: 3 });
  assert.strictEqual((await svc.handle('/friends/42/nope', 'POST')).status, 400);
  assert.strictEqual((await svc.handle('/friends/42/accept')).status, 404); // GET
  assert.strictEqual((await svc.handle('/history/1', 'POST')).status, 404);
  assert.strictEqual((await svc.handle('/friends', 'DELETE')).status, 405);
});

test('the account is passed on, and /salts asks the GC every time', async () => {
  const { svc, calls } = service({
    salts: () => ({ cluster_id: 3, metadata_salt: 1234 }),
    download: () => new Response(fixture),
  });
  const q = new URLSearchParams('account=113956776');
  assert.strictEqual((await svc.handle('/matches/115701682', 'GET', q)).status, 200);
  assert.strictEqual((await svc.handle('/salts/115701682', 'GET', q)).status, 200);
  assert.strictEqual((await svc.handle('/salts/115701682')).status, 200);
  assert.deepStrictEqual(calls.targets, [113956776, 113956776, null]);
});

function withSaltsApi(api, now = Date.now) {
  const calls = { gc: 0, urls: [], headers: [] };
  const gc = { matchSalts: async () => { calls.gc += 1; return { cluster_id: 7, metadata_salt: 77 }; } };
  const fetchFn = async (url, opts = {}) => {
    calls.urls.push(url);
    if (url.startsWith('https://dapi.test/')) {
      calls.headers.push(opts.headers || {});
      return api(url);
    }
    return new Response(fixture);
  };
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'fetcher-'));
  const svc = new Service({
    gc, cacheDir, replayUrl: config.replayUrl, saltsApi: 'https://dapi.test/', apiKey: 'k', fetch: fetchFn,
    log: { info() {}, warn() {} }, now,
  });
  return { svc, calls };
}

test('salts come from deadlock-api first; the bot quota is not touched', async () => {
  const { svc, calls } = withSaltsApi(() => Response.json({ match_id: 115701682, cluster_id: 186, metadata_salt: 386809196 }));
  const res = await svc.handle('/matches/115701682');
  assert.strictEqual(res.status, 200);
  assert.strictEqual(calls.gc, 0);
  assert.deepStrictEqual(calls.urls, [
    'https://dapi.test/v1/matches/115701682/salts',
    'http://replay186.valve.net/1422450/115701682_386809196.meta.bz2',
  ]);
  assert.deepStrictEqual(calls.headers, [{ 'X-API-KEY': 'k' }]);
});

test('when deadlock-api has no salts, is limited, or is down, the bot asks Steam', async () => {
  const answers = [
    () => Response.json({ status: 404, error: 'not found' }, { status: 404 }),
    () => Response.json({ error: 'rate limited' }, { status: 429 }),
    () => Response.json({ match_id: 1, cluster_id: null, metadata_salt: null }),
    () => { throw new Error('connect ECONNREFUSED'); },
  ];
  for (const api of answers) {
    const { svc, calls } = withSaltsApi(api);
    assert.strictEqual((await svc.handle('/matches/115701682')).status, 200);
    assert.strictEqual(calls.gc, 1);
    assert.strictEqual(calls.urls.at(-1), 'http://replay7.valve.net/1422450/115701682_77.meta.bz2');
  }
});

test('a match deadlock-api is busy with is not asked about again before Retry-After; others are', async () => {
  let t = 0;
  const busy = () => new Response('{"status":503,"error":"Steam is busy"}', { status: 503, headers: { 'retry-after': '60' } });
  const { svc, calls } = withSaltsApi(busy, () => t);
  const asked = () => calls.urls.filter((u) => u.startsWith('https://dapi.test/')).length;
  // the decoded match is cached by id, so use the salts step directly
  await svc.saltsFor(5);
  assert.strictEqual(asked(), 1);
  t += 59_000;
  await svc.saltsFor(5);
  assert.strictEqual(asked(), 1); // still backing off: straight to the bot
  await svc.saltsFor(6);
  assert.strictEqual(asked(), 2); // other matches are still tried
  t += 2_000;
  await svc.saltsFor(5);
  assert.strictEqual(asked(), 3);
  assert.strictEqual(calls.gc, 4);
});

test('salts the bot fetched are given to deadlock-api; ones from deadlock-api are not sent back', async () => {
  const posts = [];
  const make = (contributeSalts, api) => {
    const gc = { matchSalts: async () => ({ cluster_id: 7, metadata_salt: 77, replay_salt: 78 }) };
    const fetchFn = async (url, opts = {}) => {
      if (opts.method === 'POST') { posts.push([url, JSON.parse(opts.body)]); return new Response('', { status: 200 }); }
      return url.startsWith('https://dapi.test/') ? api() : new Response(fixture);
    };
    const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'fetcher-'));
    return new Service({
      gc, cacheDir, replayUrl: config.replayUrl, saltsApi: 'https://dapi.test', fetch: fetchFn, contributeSalts,
      log: { info() {}, warn() {} },
    });
  };
  const missing = () => Response.json({}, { status: 404 });
  await make(true, missing).saltsFor(5);
  await new Promise(setImmediate);
  assert.deepStrictEqual(posts, [
    ['https://dapi.test/v1/matches/salts', [{ match_id: 5, cluster_id: 7, metadata_salt: 77, replay_salt: 78 }]],
  ]);
  await make(true, () => Response.json({ cluster_id: 1, metadata_salt: 2 })).saltsFor(6);
  await make(false, missing).saltsFor(7);
  await new Promise(setImmediate);
  assert.strictEqual(posts.length, 1);
});

test('steam=0 only uses salts deadlock-api has; pages limits the history', async () => {
  const { svc, calls } = withSaltsApi(() => Response.json({}, { status: 404 }));
  const res = await svc.handle('/matches/115701682', 'GET', new URLSearchParams('steam=0'));
  assert.strictEqual(res.status, 404);
  assert.strictEqual(res.body.error, 'no_salts');
  assert.strictEqual(calls.gc, 0);

  const { svc: s2 } = service({ salts: () => null, download: () => null });
  let asked = null;
  s2.gc.fullHistory = async (id, pages) => { asked = pages; return { matches: [], complete: true }; };
  await s2.handle('/history/5', 'GET', new URLSearchParams('pages=1'));
  assert.strictEqual(asked, 1);
  await s2.handle('/history/5');
  assert.strictEqual(asked, undefined);
});
