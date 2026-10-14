'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Service } = require('../src/service');
const { GcError } = require('../src/gc');
const config = require('../src/config');
const { FriendList } = require('../src/friends');

const fixture = fs.readFileSync(path.join(__dirname, 'fixtures', '115701682.meta.bz2'));

function service({ salts, download }) {
  const calls = { salts: 0, urls: [] };
  const gc = {
    accepted: 0,
    acceptPendingRequests() { this.accepted += 1; },
    status: () => ({ gc_ready: true }),
    matchSalts: async (id) => { calls.salts += 1; return salts(id); },
    fullHistory: async () => ({ matches: [{ match_id: '115701682', hero_id: 3 }], complete: true }),
  };
  const fetchFn = async (url) => { calls.urls.push(url); return download(url); };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fetcher-'));
  const friends = new FriendList(path.join(dir, 'friends.json'));
  const cacheDir = path.join(dir, 'matches');
  return { svc: new Service({ gc, friends, cacheDir, replayUrl: config.replayUrl, fetch: fetchFn }), calls, gc, friends, dir };
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

test('friends: the web app sets the list, it survives a restart, and bad input is refused', async () => {
  const { svc, gc, friends, dir } = service({ salts: () => null, download: () => null });
  const res = await svc.handle('/friends', 'PUT', { account_ids: [7, 113956776, 7] });
  assert.deepStrictEqual(res, { status: 200, body: { account_ids: [7, 113956776] } });
  assert.ok(friends.has(113956776) && !friends.has(8));
  assert.strictEqual(gc.accepted, 1); // pending requests are re-checked against the new list
  assert.ok(new FriendList(path.join(dir, 'friends.json')).has(7));

  for (const body of [{}, { account_ids: ['7'] }, { account_ids: [-1] }, { account_ids: [2 ** 40] }]) {
    assert.strictEqual((await svc.handle('/friends', 'PUT', body)).status, 400);
  }
  assert.ok(friends.has(7)); // unchanged by the refused requests
  assert.strictEqual((await svc.handle('/friends')).status, 405);
  assert.strictEqual((await svc.handle('/history/1', 'PUT', {})).status, 405);
});
