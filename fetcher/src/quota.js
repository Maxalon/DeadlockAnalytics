'use strict';
// Rolling-window budget for requests that Steam limits per account. The bot spends it only when
// someone asks for something (a history re-pull, a match's details), so it stays well clear of
// Steam's limits instead of running into blocks. Kept on disk so a restart doesn't reset it.
//
// Limits follow deadlock-api's own ingest tool: 40 match-salt fetches and 20 friend match-history
// lookups per account per 24 h (deadlock-api-ingest, src/gc/quota.rs).
const fs = require('node:fs');

const DAY_MS = 24 * 3600 * 1000;

class RollingQuota {
  /**
   * @param {object} o
   * @param {string|null} o.file  where the timestamps are kept; null: memory only (tests)
   * @param {number} o.limit
   * @param {number} [o.windowMs]
   * @param {() => number} [o.now]
   */
  constructor({ file, limit, windowMs = DAY_MS, now = Date.now }) {
    this.file = file;
    this.limit = limit;
    this.windowMs = windowMs;
    this.now = now;
    this.used = [];
    if (file) {
      try {
        this.used = JSON.parse(fs.readFileSync(file, 'utf8')).filter(Number.isFinite);
      } catch (err) {
        if (err.code !== 'ENOENT') throw err;
      }
    }
  }

  _prune() {
    const since = this.now() - this.windowMs;
    this.used = this.used.filter((t) => t > since).sort((a, b) => a - b);
  }

  /** Spend one if there is one left. */
  take() {
    this._prune();
    if (this.used.length >= this.limit) return false;
    this.used.push(this.now());
    if (this.file) {
      fs.writeFileSync(`${this.file}.tmp`, JSON.stringify(this.used));
      fs.renameSync(`${this.file}.tmp`, this.file);
    }
    return true;
  }

  status() {
    this._prune();
    const full = this.used.length >= this.limit;
    return {
      limit: this.limit,
      remaining: this.limit - this.used.length,
      // When the oldest spent request leaves the window, i.e. the next one is free again.
      next_free_at: full ? new Date(this.used[0] + this.windowMs).toISOString() : null,
    };
  }
}

module.exports = { RollingQuota, DAY_MS };
