'use strict';
// Request handling, separate from the HTTP server and the Steam session so it is testable.
const fs = require('node:fs');
const path = require('node:path');
const { decodeMetadataFile } = require('./metadata');

const SALTS_TIMEOUT_MS = 15000;
// deadlock-api answers 429/503 (its own Steam bots are busy, or our IP's 10-per-30-min share of
// them is spent) for matches it hasn't got salts for yet. Asking again before Retry-After only
// burns that share, so each such match waits that long (or this) before deadlock-api is asked again.
const SALTS_API_BACKOFF_MS = 30 * 60 * 1000;

// Bump when decodeMetadataFile returns more: cached matches with their file are decoded again.
const DECODER_VERSION = 2;

function writeAtomic(file, data) {
  fs.writeFileSync(`${file}.tmp`, data);
  fs.renameSync(`${file}.tmp`, file);
}

const STATUS = {
  not_ready: 503, timeout: 504, private: 403, rate_limited: 429, invalid_match: 404, in_flight: 409, gc_error: 502,
  bad_request: 400, conflict: 409, quota: 429, no_salts: 404,
};

class Service {
  /**
   * @param {object} deps
   * @param {import('./gc').DeadlockGC} deps.gc
   * @param {string} deps.cacheDir
   * @param {Function} deps.replayUrl
   * @param {string} [deps.saltsApi]  deadlock-api base URL to ask for salts first; empty: only the bot
   * @param {string} [deps.apiKey]  deadlock-api key (X-API-KEY), optional
   * @param {boolean} [deps.contributeSalts]  give deadlock-api the salts the bot fetches
   * @param {typeof fetch} [deps.fetch]
   */
  constructor({ gc, cacheDir, replayUrl, saltsApi = '', apiKey = '', contributeSalts = false, fetch: fetchFn = fetch, log = console, now = Date.now }) {
    this.gc = gc;
    this.now = now;
    this._saltsApiRetryAt = new Map(); // match id -> when deadlock-api may be asked about it again
    this.saltsApi = saltsApi.replace(/\/$/, '');
    this.apiKey = apiKey;
    this.contributeSalts = contributeSalts && Boolean(this.saltsApi);
    this.log = log;
    this.cacheDir = cacheDir;
    this.replayUrl = replayUrl;
    this.fetch = fetchFn;
    fs.mkdirSync(cacheDir, { recursive: true });
  }

  health() {
    return { status: 200, body: this.gc.status() };
  }

  /** `pages`: how many history pages (each one Steam request) to follow; default all of them. */
  async history(accountId, pages = undefined) {
    const { matches, complete } = await this.gc.fullHistory(accountId, pages);
    return {
      status: 200,
      body: {
        account_id: accountId,
        complete,
        matches: matches.map((m) => ({ ...m, match_id: Number(m.match_id) })),
      },
    };
  }

  /** `steam: false`: only salts deadlock-api already has; never spends the bot's Steam budget. */
  async match(matchId, targetAccountId = null, { steam = true } = {}) {
    // Finished matches never change, so a decoded match is cached forever. The file itself is
    // kept too: when the decoder learns to read more of it, cached matches are decoded again
    // instead of spending salts on them a second time.
    const cached = path.join(this.cacheDir, `${matchId}.json`);
    const raw = path.join(this.cacheDir, `${matchId}.meta`);
    if (fs.existsSync(cached)) {
      const match = JSON.parse(fs.readFileSync(cached, 'utf8'));
      if ((match.decoder || 1) >= DECODER_VERSION || !fs.existsSync(raw)) return { status: 200, body: match };
      return { status: 200, body: this._decode(matchId, fs.readFileSync(raw)) };
    }

    const salts = await this.saltsFor(matchId, targetAccountId, { steam });
    const url = this.replayUrl(salts.cluster_id, matchId, salts.metadata_salt);
    const resp = await this.fetch(url);
    if (resp.status === 404) return { status: 404, body: { error: 'gone', message: `Valve no longer serves the files of match ${matchId}` } };
    if (!resp.ok) return { status: 502, body: { error: 'download_failed', message: `${url} -> HTTP ${resp.status}` } };
    const file = Buffer.from(await resp.arrayBuffer());
    const match = this._decode(matchId, file);
    writeAtomic(raw, file);
    return { status: 200, body: match };
  }

  _decode(matchId, file) {
    const match = { ...decodeMetadataFile(file), decoder: DECODER_VERSION };
    writeAtomic(path.join(this.cacheDir, `${matchId}.json`), JSON.stringify(match));
    return match;
  }

  /**
   * The salts for a match's files. Only these are rate-limited (per Steam account, ~17 per half
   * hour), the download from Valve's replay servers isn't. deadlock-api already has the salts of a
   * great many matches and serves those freely (and fetches others with its own bots, under its own
   * limit), so ask it first and use the bot's quota only for what it can't give.
   */
  async saltsFor(matchId, targetAccountId, { steam = true } = {}) {
    const retryAt = this._saltsApiRetryAt.get(matchId) || 0;
    if (this.saltsApi && this.now() >= retryAt) {
      try {
        const resp = await this.fetch(`${this.saltsApi}/v1/matches/${matchId}/salts`, {
          headers: this.apiKey ? { 'X-API-KEY': this.apiKey } : {},
          signal: AbortSignal.timeout(SALTS_TIMEOUT_MS),
        });
        if (resp.ok) {
          const s = await resp.json();
          if (s.cluster_id && s.metadata_salt) {
            this._saltsApiRetryAt.delete(matchId);
            return { cluster_id: s.cluster_id, metadata_salt: s.metadata_salt };
          }
        } else if (resp.status === 429 || resp.status === 503) {
          const after = Number(resp.headers.get('retry-after'));
          const wait = Number.isFinite(after) && after > 0 ? after * 1000 : SALTS_API_BACKOFF_MS;
          this._saltsApiRetryAt.set(matchId, this.now() + wait);
          this.log.info(`deadlock-api can't get salts for ${matchId} now (HTTP ${resp.status}); not asking it again for ${Math.round(wait / 60000)} min`);
        } else {
          this.log.info(`deadlock-api has no salts for ${matchId} (HTTP ${resp.status}); asking Steam`);
        }
      } catch (err) {
        this.log.warn(`deadlock-api salts for ${matchId} failed (${err.message}); asking Steam`);
      }
    }
    if (!steam) throw Object.assign(new Error(`deadlock-api has no salts for ${matchId} (yet)`), { code: 'no_salts' });
    const salts = await this.gc.matchSalts(matchId, targetAccountId);
    if (this.contributeSalts) this._contribute(matchId, salts); // not awaited: the match doesn't wait on it
    return salts;
  }

  /**
   * Give deadlock-api the salts the bot fetched, like its own ingest tool does: it then has the
   * match too, for everyone, and later lookups of it cost no budget.
   */
  async _contribute(matchId, salts) {
    try {
      const resp = await this.fetch(`${this.saltsApi}/v1/matches/salts`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'user-agent': 'DeadlockAnalytics (self-hosted)' },
        body: JSON.stringify([{
          match_id: matchId, cluster_id: salts.cluster_id, metadata_salt: salts.metadata_salt, replay_salt: salts.replay_salt,
        }]),
        signal: AbortSignal.timeout(SALTS_TIMEOUT_MS),
      });
      if (resp.ok) this.log.info(`gave deadlock-api the salts of match ${matchId}`);
      else this.log.warn(`deadlock-api refused the salts of match ${matchId}: HTTP ${resp.status}`);
    } catch (err) {
      this.log.warn(`giving deadlock-api the salts of match ${matchId} failed: ${err.message}`);
    }
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
      if (pathname === '/profiles') {
        const ids = (query.get('ids') || '').split(',').filter((x) => /^\d{1,10}$/.test(x)).map(Number);
        if (!ids.length || ids.length > 100) return { status: 400, body: { error: 'bad_request', message: 'ids: 1 to 100 SteamID3s' } };
        return { status: 200, body: { profiles: await this.gc.profiles(ids) } };
      }
      if ((m = pathname.match(/^\/history\/(\d{1,10})$/))) {
        const pages = /^\d{1,3}$/.test(query.get('pages') || '') ? Math.max(Number(query.get('pages')), 1) : undefined;
        return await this.history(Number(m[1]), pages);
      }
      if ((m = pathname.match(/^\/matches\/(\d{1,15})$/))) {
        return await this.match(Number(m[1]), account, { steam: query.get('steam') !== '0' });
      }
      if ((m = pathname.match(/^\/salts\/(\d{1,15})$/))) return await this.salts(Number(m[1]), account, query.get('force') === '1');
      return { status: 404, body: { error: 'not_found' } };
    } catch (err) {
      const status = STATUS[err.code] || 500;
      return { status, body: { error: err.code || 'internal', message: err.message } };
    }
  }
}

module.exports = { Service };
