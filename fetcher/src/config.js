'use strict';
const path = require('node:path');

const dataDir = process.env.FETCHER_DATA_DIR || '/data';

module.exports = {
  port: Number(process.env.FETCHER_PORT || 8001),
  dataDir,
  refreshTokenFile: path.join(dataDir, 'refresh_token'),
  matchCacheDir: path.join(dataDir, 'matches'),
  // The tracked accounts, as last sent by the web app: only their friend requests are accepted.
  friendsFile: path.join(dataDir, 'friends.json'),
  replayUrl: (clusterId, matchId, salt) => `http://replay${clusterId}.valve.net/1422450/${matchId}_${salt}.meta.bz2`,
};
