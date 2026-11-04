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
  buildsQuotaFile: path.join(dataDir, 'quota-builds.json'),
  buildPublishesPerDay: Number(process.env.BOT_BUILD_PUBLISHES_PER_DAY || 20),
  // Ask deadlock-api for match salts before spending the bot's rate-limited quota.
  // SALTS_FROM_DEADLOCK_API=0 turns that off.
  saltsApi: process.env.SALTS_FROM_DEADLOCK_API === '0' ? '' : (process.env.DEADLOCK_API_BASE || 'https://api.deadlock-api.com'),
  apiKey: process.env.DEADLOCK_API_KEY || '',
  // Give deadlock-api the salts the bot fetches, in return for using it. CONTRIBUTE_SALTS=0 turns it off.
  contributeSalts: process.env.CONTRIBUTE_SALTS !== '0',
  // Watchdog: restart the fetcher after this long without a GC session (unless it waits on purpose,
  // e.g. for a new login), and check every this many minutes that Steam still answers.
  maxNotReadyMinutes: Number(process.env.BOT_MAX_OFFLINE_MINUTES || 15),
  probeEveryMinutes: Math.max(1, Number(process.env.BOT_PROBE_MINUTES || 5)),
  replayUrl: (clusterId, matchId, salt) => `http://replay${clusterId}.valve.net/1422450/${matchId}_${salt}.meta.bz2`,
};
