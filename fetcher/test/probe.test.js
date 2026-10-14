'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { probe } = require('../src/probe');

// A fake Steam: allows `allowed` requests, then blocks for `blockS` seconds of fake time.
function fakeFetcher({ allowed, blockS }) {
  let t = 0;
  let used = 0;
  let blockedUntil = null;
  const paths = [];
  return {
    paths,
    now: () => t,
    sleep: async (ms) => { t += ms; },
    get: async (path) => {
      paths.push(path);
      if (path.startsWith('/history/')) return { status: 200, body: { matches: [1, 3, 2, 5, 4].map((match_id) => ({ match_id })) } };
      t += 1500; // the fetcher's own spacing
      if (blockedUntil !== null && t < blockedUntil) return { status: 429, body: { error: 'rate_limited' } };
      if (blockedUntil !== null) { blockedUntil = null; used = 0; }
      if (used >= allowed) { blockedUntil = t + blockS * 1000; return { status: 429, body: { error: 'rate_limited' } }; }
      used += 1;
      return { status: 200, body: {} };
    },
  };
}

test('probe counts requests until the limit and measures the block', async () => {
  const f = fakeFetcher({ allowed: 3, blockS: 100 });
  const summary = await probe({ ...f, accountId: 7, gapS: 2, target: true, log: () => {} });
  assert.strictEqual(summary.requests_ok, 3);
  assert.ok(summary.blocked_seconds >= 100 && summary.blocked_seconds < 140, summary.blocked_seconds);
  assert.deepStrictEqual(f.paths.slice(1, 4), ['/salts/5?account=7', '/salts/4?account=7', '/salts/3?account=7']); // newest first
});

test('probe without --target, and without hitting a limit', async () => {
  const f = fakeFetcher({ allowed: 99, blockS: 0 });
  const summary = await probe({ ...f, accountId: 7, gapS: 0, target: false, log: () => {} });
  assert.deepStrictEqual([summary.requests_ok, summary.blocked_seconds], [5, null]);
  assert.strictEqual(f.paths[1], '/salts/5');
});
