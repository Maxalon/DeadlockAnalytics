'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { probe } = require('../src/probe');

// A fake fetcher in fake time. `allow(t)` decides whether a request at time t gets through.
function fakeFetcher(allow) {
  let t = 1_800_000_000_000;
  const paths = [];
  return {
    paths,
    now: () => t,
    sleep: async (ms) => { t += ms; },
    get: async (path) => {
      paths.push(path);
      if (path.startsWith('/history/')) return { status: 200, body: { matches: [1, 3, 2].map((match_id) => ({ match_id })) } };
      t += 1500; // the fetcher's own spacing
      return allow(t) ? { status: 200, body: {} } : { status: 429, body: { error: 'rate_limited' } };
    },
  };
}

// Sliding window: at most n accepted requests in any w ms. Refused ones don't count.
function slidingWindow(n, w) {
  const accepted = [];
  return (t) => {
    while (accepted.length && accepted[0] <= t - w) accepted.shift();
    if (accepted.length >= n) return false;
    accepted.push(t);
    return true;
  };
}

// Token bucket: capacity n, one token back every `every` ms.
function tokenBucket(n, every) {
  let tokens = n;
  let last = null;
  return (t) => {
    if (last !== null) tokens = Math.min(n, tokens + Math.floor((t - last) / every));
    if (last === null || t - last >= every) last = t;
    if (tokens < 1) return false;
    tokens -= 1;
    return true;
  };
}

const run = (f, opts) => probe({ ...f, accountId: 7, log: () => {}, ...opts });

test('sliding window: every cycle gets the full quota, the block is measured to the retry interval', async () => {
  const f = fakeFetcher(slidingWindow(4, 100_000));
  const cycles = await run(f, { cycles: 3, retryS: 5 });
  assert.deepStrictEqual(cycles.map((c) => c.requests_ok), [4, 4, 4]);
  for (const c of cycles) assert.ok(c.blocked_s >= 90 && c.blocked_s <= 100, `${c.blocked_s}`);
  assert.strictEqual(f.paths[1], '/salts/3'); // newest first
});

test('token bucket: after the first block only one request per refill gets through', async () => {
  const f = fakeFetcher(tokenBucket(4, 60_000));
  const cycles = await run(f, { cycles: 3, retryS: 5 });
  assert.strictEqual(cycles[0].requests_ok, 4);
  assert.deepStrictEqual(cycles.slice(1).map((c) => c.requests_ok), [1, 1]);
});

test('alternate flips target_account_id every cycle', async () => {
  const f = fakeFetcher(slidingWindow(2, 20_000));
  const cycles = await run(f, { cycles: 3, retryS: 5, target: true, alternate: true });
  assert.deepStrictEqual(cycles.map((c) => c.target), [true, false, true]);
  assert.ok(f.paths.includes('/salts/3?account=7') && f.paths.includes('/salts/2'));
});

test('no limit: stops instead of hammering forever', async () => {
  const f = fakeFetcher(() => true);
  const [c] = await run(f, { cycles: 2 });
  assert.strictEqual(c.limited_at, null);
  assert.ok(c.requests_ok > 500);
});
