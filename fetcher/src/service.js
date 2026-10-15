'use strict';
// Request handling, separate from the HTTP server and the Steam session so it is testable.
const fs = require('node:fs');
const path = require('node:path');
const { decodeMetadataFile } = require('./metadata');

const STATUS = {
  not_ready: 503, timeout: 504, private: 403, rate_limited: 429, invalid_match: 404, in_flight: 409, gc_error: 502,
  bad_request: 400, conflict: 409,
};

class Service {
  /**
   * @param {{gc: import('./gc').DeadlockGC, cacheDir: string, replayUrl: Function, fetch?: typeof fetch}} deps
   */
  constructor({ gc, cacheDir, replayUrl, fetch: fetchFn = fetch }) {
    this.gc = gc;
    this.cacheDir = cacheDir;
    this.replayUrl = replayUrl;
    this.fetch = fetchFn;
    fs.mkdirSync(cacheDir, { recursive: true });
  }

  health() {
    return { status: 200, body: this.gc.status() };
  }

  async history(accountId) {
    const { matches, complete } = await this.gc.fullHistory(accountId);
    return {
      status: 200,
      body: {
        account_id: accountId,
        complete,
        matches: matches.map((m) => ({ ...m, match_id: Number(m.match_id) })),
      },
    };
  }

  async match(matchId, targetAccountId = null) {
    // Finished matches never change, so a decoded match is cached forever.
    const cached = path.join(this.cacheDir, `${matchId}.json`);
    if (fs.existsSync(cached)) return { status: 200, body: JSON.parse(fs.readFileSync(cached, 'utf8')) };

    const salts = await this.gc.matchSalts(matchId, targetAccountId);
    const url = this.replayUrl(salts.cluster_id, matchId, salts.metadata_salt);
    const resp = await this.fetch(url);
    if (resp.status === 404) return { status: 404, body: { error: 'gone', message: `Valve no longer serves the files of match ${matchId}` } };
    if (!resp.ok) return { status: 502, body: { error: 'download_failed', message: `${url} -> HTTP ${resp.status}` } };
    const match = decodeMetadataFile(Buffer.from(await resp.arrayBuffer()));
    fs.writeFileSync(`${cached}.tmp`, JSON.stringify(match));
    fs.renameSync(`${cached}.tmp`, cached);
    return { status: 200, body: match };
  }

  /** Only the GC request for a match's salts, never cached: for measuring Steam's rate limit. */
  async salts(matchId, targetAccountId = null, force = false) {
    return { status: 200, body: await this.gc.matchSalts(matchId, targetAccountId, { force }) };
  }

  /** Route a request to a handler; errors from the GC become HTTP statuses. */
  async handle(pathname, method = 'GET', query = new URLSearchParams()) {
    const account = /^\d{1,10}$/.test(query.get('account') || '') ? Number(query.get('account')) : null;
    try {
      let m;
      if (method === 'POST') {
        if (pathname === '/friends/decline-all') return { status: 200, body: { declined: this.gc.declineAll() } };
        if ((m = pathname.match(/^\/friends\/(\d{1,10})\/([a-z]+)$/))) {
          await this.gc.friendAction(Number(m[1]), m[2]);
          return { status: 200, body: this.gc.friends() };
        }
        return { status: 404, body: { error: 'not_found' } };
      }
      if (method !== 'GET') return { status: 405, body: { error: 'method_not_allowed' } };
      if (pathname === '/friends') return { status: 200, body: this.gc.friends() };
      if (pathname === '/health') return this.health();
      if ((m = pathname.match(/^\/history\/(\d{1,10})$/))) return await this.history(Number(m[1]));
      if ((m = pathname.match(/^\/matches\/(\d{1,15})$/))) return await this.match(Number(m[1]), account);
      if ((m = pathname.match(/^\/salts\/(\d{1,15})$/))) return await this.salts(Number(m[1]), account, query.get('force') === '1');
      return { status: 404, body: { error: 'not_found' } };
    } catch (err) {
      const status = STATUS[err.code] || 500;
      return { status, body: { error: err.code || 'internal', message: err.message } };
    }
  }
}

module.exports = { Service };
