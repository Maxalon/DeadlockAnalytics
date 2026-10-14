'use strict';
const path = require('node:path');

const ids = (raw) => new Set(String(raw || '').split(/[,;\s]+/).filter(Boolean).map((x) => {
  const n = BigInt(x);
  // Accept SteamID64 as well as SteamID3.
  return Number(n >= 76561197960265728n ? n - 76561197960265728n : n);
}));

const dataDir = process.env.FETCHER_DATA_DIR || '/data';

module.exports = {
  port: Number(process.env.FETCHER_PORT || 8001),
  dataDir,
  refreshTokenFile: path.join(dataDir, 'refresh_token'),
  matchCacheDir: path.join(dataDir, 'matches'),
  // Only these accounts' friend requests are accepted (the players you track).
  friendAccountIds: () => ids(process.env.FRIEND_ACCOUNT_IDS || process.env.TRACKED_ACCOUNT_IDS),
  replayUrl: (clusterId, matchId, salt) => `http://replay${clusterId}.valve.net/1422450/${matchId}_${salt}.meta.bz2`,
};
