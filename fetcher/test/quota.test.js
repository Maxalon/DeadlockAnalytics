'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { RollingQuota } = require('../src/quota');

test('rolling budget: spends, refuses when used up, frees as the window rolls, survives a restart', () => {
  let t = 1_000_000;
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'quota-')), 'q.json');
  const q = new RollingQuota({ file, limit: 2, windowMs: 100_000, now: () => t });
  assert.ok(q.take());
  t += 10_000;
  assert.ok(q.take());
  assert.strictEqual(q.take(), false);
  assert.deepStrictEqual(q.status(), { limit: 2, remaining: 0, next_free_at: new Date(1_100_000).toISOString() });

  const again = new RollingQuota({ file, limit: 2, windowMs: 100_000, now: () => t });
  assert.strictEqual(again.take(), false); // a restart doesn't hand out a fresh budget
  t = 1_100_001; // the first one left the window
  assert.ok(again.take());
  assert.strictEqual(again.status().remaining, 0);
});
