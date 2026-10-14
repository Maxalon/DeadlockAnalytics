'use strict';
// Decodes Valve's <match_id>_<salt>.meta.bz2 files into the same JSON shape the site already
// reads from deadlock-api's /v1/matches/metadata endpoint, so it can store either unchanged.
const zlib = require('node:zlib');
const bzip2 = require('seek-bzip');
const proto = require('./proto');

const TEAMS = { 0: 'Team0', 1: 'Team1', 16: 'Spectator' };
const MATCH_MODES = {
  0: 'Invalid', 1: 'Unranked', 2: 'PrivateLobby', 3: 'CoopBot', 4: 'Ranked',
  5: 'ServerTest', 6: 'Tutorial', 7: 'HeroLabs', 8: 'NewPlayerPlacement',
};
const GAME_MODES = {
  0: 'Invalid', 1: 'Normal', 2: 'OneVsOneTest', 3: 'Sandbox', 4: 'StreetBrawl', 5: 'ExploreNYC', 6: 'Internal',
};

function averageBadge(a, b) {
  // Same mapping deadlock-api uses: badges (tier*10 + subrank) onto a contiguous scale, averaged.
  if (!a || !b) return null;
  const flat = (x) => Math.floor(x / 10) * 6 + (x % 10);
  const avg = Math.round((flat(a) + flat(b)) / 2);
  return Math.floor(avg / 6) * 10 + (avg % 6);
}

function maxOf(stats, field) {
  return stats.reduce((m, s) => Math.max(m, s[field] || 0), 0);
}

// Despite the .bz2 name, Valve serves these files zstd-compressed; older ones were bzip2.
function decompress(file) {
  if (file.subarray(0, 4).equals(Buffer.from([0x28, 0xb5, 0x2f, 0xfd]))) return zlib.zstdDecompressSync(file);
  if (file.subarray(0, 3).toString('latin1') === 'BZh') return Buffer.from(bzip2.decode(file));
  throw new Error(`unknown compression (first bytes ${file.subarray(0, 4).toString('hex')})`);
}

function decodeMetadataFile(file) {
  const raw = decompress(file);
  const meta = proto.MatchMetaData.decode(raw);
  const contents = proto.toObject(proto.MatchMetaDataContents, proto.MatchMetaDataContents.decode(meta.match_details));
  const info = contents.match_info;
  if (!info || !info.players.length) throw new Error('metadata has no match info');
  return {
    match_id: Number(info.match_id || meta.match_id),
    start_time: info.start_time,
    duration_s: info.duration_s,
    winning_team: TEAMS[info.winning_team] ?? String(info.winning_team),
    match_mode: MATCH_MODES[info.match_mode] ?? String(info.match_mode),
    game_mode: GAME_MODES[info.game_mode] ?? String(info.game_mode),
    average_badge: averageBadge(info.average_badge_team0, info.average_badge_team1),
    players: info.players.map((p) => ({
      account_id: p.account_id,
      player_slot: p.player_slot,
      team: TEAMS[p.team] ?? String(p.team),
      hero_id: p.hero_id,
      kills: p.kills,
      deaths: p.deaths,
      assists: p.assists,
      net_worth: p.net_worth,
      // The stats are cumulative snapshots over the match; the max is the final total.
      max_player_damage: maxOf(p.stats, 'player_damage'),
      max_boss_damage: maxOf(p.stats, 'boss_damage'),
      max_teammate_healing: maxOf(p.stats, 'teammate_healing'),
      mvp_rank: p.mvp_rank || null,
      abandon_match_time_s: p.abandon_match_time_s,
    })),
  };
}

module.exports = { decodeMetadataFile, averageBadge };
