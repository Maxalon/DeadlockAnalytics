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
