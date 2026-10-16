'use strict';
const path = require('node:path');

const dataDir = process.env.FETCHER_DATA_DIR || '/data';

module.exports = {
  port: Number(process.env.FETCHER_PORT || 8001),
  dataDir,
  refreshTokenFile: path.join(dataDir, 'refresh_token'),
  matchCacheDir: path.join(dataDir, 'matches'),
  // Ask deadlock-api for match salts before spending the bot's rate-limited quota.
  // SALTS_FROM_DEADLOCK_API=0 turns that off.
  saltsApi: process.env.SALTS_FROM_DEADLOCK_API === '0' ? '' : (process.env.DEADLOCK_API_BASE || 'https://api.deadlock-api.com'),
  apiKey: process.env.DEADLOCK_API_KEY || '',
  replayUrl: (clusterId, matchId, salt) => `http://replay${clusterId}.valve.net/1422450/${matchId}_${salt}.meta.bz2`,
};
