'use strict';
const path = require('node:path');

const dataDir = process.env.FETCHER_DATA_DIR || '/data';

module.exports = {
  port: Number(process.env.FETCHER_PORT || 8001),
  dataDir,
  refreshTokenFile: path.join(dataDir, 'refresh_token'),
  matchCacheDir: path.join(dataDir, 'matches'),
  saltsQuotaFile: path.join(dataDir, 'quota-salts.json'),
  historyQuotaFile: path.join(dataDir, 'quota-history.json'),
  // Per 24 h, like deadlock-api's ingest tool. Override only if you know Steam allows more.
  saltsPerDay: Number(process.env.BOT_SALTS_PER_DAY || 40),
  historyPagesPerDay: Number(process.env.BOT_HISTORY_PAGES_PER_DAY || 20),
  // Ask deadlock-api for match salts before spending the bot's rate-limited quota.
  // SALTS_FROM_DEADLOCK_API=0 turns that off.
  saltsApi: process.env.SALTS_FROM_DEADLOCK_API === '0' ? '' : (process.env.DEADLOCK_API_BASE || 'https://api.deadlock-api.com'),
  apiKey: process.env.DEADLOCK_API_KEY || '',
  // Give deadlock-api the salts the bot fetches, in return for using it. CONTRIBUTE_SALTS=0 turns it off.
  contributeSalts: process.env.CONTRIBUTE_SALTS !== '0',
  replayUrl: (clusterId, matchId, salt) => `http://replay${clusterId}.valve.net/1422450/${matchId}_${salt}.meta.bz2`,
};
