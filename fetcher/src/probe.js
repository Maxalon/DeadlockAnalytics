'use strict';
// Measures Steam's rate limit on match metadata requests (the GC request every match file
// download needs). Each cycle: request as fast as the fetcher allows until Steam refuses, then
// retry every few seconds until it accepts again. Over several cycles this shows
//   - the quota (requests per cycle) and how long a block lasts,
//   - what kind of limit it is: after an unblock, a sliding window lets the whole quota through
//     again at once, a token bucket only one request at a time; a fixed window unblocks at round
//     clock times,
//   - with --alternate, whether sending target_account_id (as the game does) changes the quota.
// Talks to the running fetcher, like the CLI. Stop the web app meanwhile: its syncs use the same quota.
// The probe asks Steam even while the fetcher pauses after a rate limit (force=1), so keep the
// retry interval generous: refused requests may count against the limit and prolong the block.

const MAX_BLOCKED_S = 6 * 3600;
// GC answers that aren't about the rate limit (match unknown or still being processed): the
// request went through, so it counts. Anything else (bot offline, timeout) makes the
// measurement meaningless, so the probe stops.
const COUNTED = new Set([200, 404, 409]);

/**
 * @param {object} o
 * @param {(path: string) => Promise<{status: number, body: any}>} o.get  GET on the fetcher
 * @param {number} o.accountId  a tracked player whose history to take match ids from
 * @param {number} [o.gapS]  extra pause between requests (the fetcher itself waits >= 1.5 s)
 * @param {boolean} [o.target]  send target_account_id like the game does (first cycle)
 * @param {boolean} [o.alternate]  flip `target` every cycle
 * @param {number} [o.cycles]  burst-until-blocked + wait-until-unblocked rounds
 * @param {number} [o.retryS]  how often to retry while blocked (the block end is measured to this)
 * @param {(line: string) => void} o.log
 * @param {(ms: number) => Promise<void>} [o.sleep]
 * @param {() => number} [o.now]
 * @returns {Promise<object[]>} one summary per cycle
 */
async function probe({
  get, accountId, gapS = 0, target = false, alternate = false, cycles = 1, retryS = 300, log,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)), now = Date.now,
}) {
  const history = await get(`/history/${accountId}`);
  if (history.status !== 200) throw new Error(`history: ${history.status} ${JSON.stringify(history.body)}`);
  const ids = history.body.matches.map((m) => m.match_id).sort((a, b) => b - a);
  if (!ids.length) throw new Error('no matches in the history to probe with');
  // Salts are never cached, so the same ids can be asked for again in later cycles.
  let next = 0;
  const nextId = () => ids[next++ % ids.length];
  const iso = (t) => new Date(t).toISOString();
  const seconds = (from, to = now()) => Number(((to - from) / 1000).toFixed(1));

  log(`probing with ${ids.length} matches of ${accountId}: ${cycles} cycle(s), ${gapS}s between requests, retry every ${retryS}s while blocked`);
  const results = [];
  let sendTarget = target;
  let pending = null; // a request that already went through while checking for the unblock
  for (let cycle = 1; cycle <= cycles; cycle++) {
    const query = sendTarget ? `?force=1&account=${accountId}` : '?force=1';
    const salts = async (id) => {
      const resp = await get(`/salts/${id}${query}`);
      if (resp.status !== 429 && !COUNTED.has(resp.status)) {
        throw new Error(`fetcher answered ${resp.status} ${JSON.stringify(resp.body)}; stopping, the measurement would be wrong`);
      }
      return resp;
    };
    const start = pending ?? now();
    let ok = pending === null ? 0 : 1;
    pending = null;
    let limited = null;
    while (limited === null) {
      const id = nextId();
      const resp = await salts(id);
      if (resp.status === 429) {
        limited = { id, at: now() };
        break;
      }
      if (resp.status !== 200) log(`  ${id}: ${resp.status} ${resp.body.error} (a GC answer, counted)`);
      ok += 1;
      if (ok > 500) break; // no limit worth measuring
      await sleep(gapS * 1000);
    }
    const summary = {
      cycle, target: sendTarget, requests_ok: ok, burst_s: seconds(start, limited ? limited.at : now()),
      started_at: iso(start), limited_at: limited ? iso(limited.at) : null, blocked_s: null, unblocked_at: null,
    };
    if (!limited) {
      log(`cycle ${cycle}: no rate limit after ${ok} requests`);
      results.push(summary);
      log(JSON.stringify(summary));
      break;
    }
    log(`cycle ${cycle}: ${ok} ok in ${summary.burst_s}s, limited at ${summary.limited_at}`);
    while (seconds(limited.at) < MAX_BLOCKED_S) {
      await sleep(retryS * 1000);
      const resp = await salts(limited.id);
      if (resp.status !== 429) {
        summary.blocked_s = seconds(limited.at);
        summary.unblocked_at = iso(now());
        pending = now(); // this request counts toward the next cycle
        break;
      }
    }
    results.push(summary);
    log(summary.blocked_s === null ? `still blocked after ${MAX_BLOCKED_S}s; stopping` : `unblocked after ${summary.blocked_s}s at ${summary.unblocked_at}`);
    log(JSON.stringify(summary));
    if (summary.blocked_s === null) break;
    if (alternate) sendTarget = !sendTarget;
  }
  return results;
}

module.exports = { probe };
