'use strict';
// Measures Steam's rate limit on match metadata requests (the GC request every match file
// download needs): how many requests in a row go through at a given pace, and how long Steam
// then blocks. Talks to the running fetcher, like the CLI.

const RETRY_EVERY_S = 30;
const MAX_BLOCKED_S = 2 * 3600;

/**
 * @param {object} o
 * @param {(path: string) => Promise<{status: number, body: any}>} o.get  GET on the fetcher
 * @param {number} o.accountId  a tracked player whose history to take match ids from
 * @param {number} o.gapS  extra pause between requests (the fetcher itself waits >= 1.5 s)
 * @param {boolean} o.target  send target_account_id like the game does
 * @param {(line: string) => void} o.log
 * @param {(ms: number) => Promise<void>} [o.sleep]
 * @param {() => number} [o.now]
 */
async function probe({ get, accountId, gapS, target, log, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), now = Date.now }) {
  const history = await get(`/history/${accountId}`);
  if (history.status !== 200) throw new Error(`history: ${history.status} ${JSON.stringify(history.body)}`);
  const ids = history.body.matches.map((m) => m.match_id).sort((a, b) => b - a);
  const query = target ? `?account=${accountId}` : '';
  const salts = (id) => get(`/salts/${id}${query}`);
  const seconds = (t) => ((now() - t) / 1000).toFixed(1);

  log(`probing ${ids.length} matches of ${accountId}, ${gapS}s apart, target_account_id ${target ? 'sent' : 'not sent'}`);
  const start = now();
  let ok = 0;
  let limitedId = null;
  for (const id of ids) {
    const resp = await salts(id);
    if (resp.status === 429) {
      limitedId = id;
      break;
    }
    ok += 1;
    log(`  #${ok} ${id} +${seconds(start)}s ${resp.status === 200 ? 'ok' : `${resp.status} ${resp.body.error}`}`);
    await sleep(gapS * 1000);
  }
  const summary = { requests_ok: ok, seconds_until_limited: null, blocked_seconds: null, gap_s: gapS, target };
  if (limitedId === null) {
    log(`no rate limit after ${ok} requests in ${seconds(start)}s`);
    return summary;
  }
  summary.seconds_until_limited = Number(seconds(start));
  log(`rate limited after ${ok} requests in ${summary.seconds_until_limited}s; retrying every ${RETRY_EVERY_S}s`);

  const limitedAt = now();
  while ((now() - limitedAt) / 1000 < MAX_BLOCKED_S) {
    await sleep(RETRY_EVERY_S * 1000);
    const resp = await salts(limitedId);
    if (resp.status !== 429) {
      summary.blocked_seconds = Number(seconds(limitedAt));
      log(`unblocked after ~${summary.blocked_seconds}s`);
      return summary;
    }
    log(`  still limited at +${seconds(limitedAt)}s`);
  }
  log(`still limited after ${MAX_BLOCKED_S}s; giving up`);
  return summary;
}

module.exports = { probe };
