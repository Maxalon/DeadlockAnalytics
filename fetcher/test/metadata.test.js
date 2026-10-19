'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { decodeMetadataFile } = require('../src/metadata');

// Match 115701682 as Valve serves it (zstd, despite the name). Expected values are the ones
// deadlock-api returns for this match (see research/mvp-115701682).
const fixture = (name) => fs.readFileSync(path.join(__dirname, 'fixtures', name));
const match = decodeMetadataFile(fixture('115701682.meta.bz2'));

test('bzip2-compressed files decode to the same match', () => {
  assert.deepStrictEqual(decodeMetadataFile(fixture('115701682.bzip2.meta.bz2')), match);
});

test('unknown compression is an error', () => {
  assert.throws(() => decodeMetadataFile(Buffer.from('<html>')), /unknown compression/);
});

test('match info', () => {
  assert.strictEqual(match.match_id, 115701682);
  assert.strictEqual(match.duration_s, 1613);
  assert.strictEqual(match.start_time, 1791848958);
  assert.strictEqual(match.winning_team, 'Team1');
  assert.strictEqual(match.match_mode, 'Unranked');
  assert.strictEqual(match.game_mode, 'Normal');
  assert.strictEqual(match.players.length, 12);
});

test('player totals match deadlock-api', () => {
  const bySlot = Object.fromEntries(match.players.map((p) => [p.player_slot, p]));
  assert.deepStrictEqual(
    { ...bySlot[7] },
    {
      account_id: 894295721, player_slot: 7, team: 'Team1', hero_id: 77, kills: 10, deaths: 1, assists: 21,
      net_worth: 30419, max_player_damage: 29782, max_boss_damage: 7641, max_teammate_healing: 0, mvp_rank: 1,
      abandon_match_time_s: 0, badge: null,
    },
  );
  assert.strictEqual(bySlot[12].account_id, 113956776);
  assert.strictEqual(bySlot[12].max_player_damage, 33681);
  assert.strictEqual(bySlot[12].max_boss_damage, 3244);
  assert.strictEqual(bySlot[12].mvp_rank, 2);
  assert.strictEqual(match.players.filter((p) => p.mvp_rank).length, 3);
});

test('unranked match: no badges, pool flags off', () => {
  assert.strictEqual(match.players.filter((p) => p.badge).length, 0);
  assert.strictEqual(match.average_badge_team0, null);
  assert.deepStrictEqual([match.low_pri_pool, match.new_player_pool, match.is_high_skill_range_parties], [false, false, false]);
});

test('ranked match: each player\'s badge going in and the matchmaking pool', () => {
  const zlib = require('node:zlib');
  const proto = require('../src/proto');
  const player = (slot, badge) => ({
    account_id: 1000 + slot, player_slot: slot, team: slot % 2, hero_id: 7, player_rank_data: badge ? { initial_display_rank: badge } : null,
  });
  const contents = proto.MatchMetaDataContents.encode(proto.MatchMetaDataContents.create({
    match_info: {
      match_id: 1, duration_s: 1800, winning_team: 0, start_time: 1792000000, game_mode: 1, match_mode: 4,
      average_badge_team0: 63, average_badge_team1: 62, low_pri_pool: true,
      players: [player(1, 64), player(2, 0)],
    },
  })).finish();
  const file = zlib.zstdCompressSync(proto.MatchMetaData.encode(proto.MatchMetaData.create({ match_id: 1, match_details: contents })).finish());
  const ranked = decodeMetadataFile(file);
  assert.strictEqual(ranked.match_mode, 'Ranked');
  assert.deepStrictEqual(ranked.players.map((p) => p.badge), [64, null]);
  assert.deepStrictEqual([ranked.average_badge_team0, ranked.average_badge_team1, ranked.average_badge], [63, 62, 63]);
  assert.strictEqual(ranked.low_pri_pool, true);
});

test('badge average: subranks 1-6 on one scale; from the players when Valve leaves team averages at 0', () => {
  const { averageBadge } = require('../src/metadata');
  assert.strictEqual(averageBadge(26, 26), 26); // Seeker 6 stays Seeker 6
  assert.strictEqual(averageBadge(26, 31), 31); // neighbours
  assert.strictEqual(averageBadge(0, null), null);
  const zlib = require('node:zlib');
  const proto = require('../src/proto');
  const contents = proto.MatchMetaDataContents.encode(proto.MatchMetaDataContents.create({
    match_info: {
      match_id: 2, match_mode: 4, average_badge_team0: 0, average_badge_team1: 0,
      players: [44, 46, 0].map((b, i) => ({ account_id: i + 1, player_slot: i + 1, player_rank_data: b ? { initial_display_rank: b } : null })),
    },
  })).finish();
  const file = zlib.zstdCompressSync(proto.MatchMetaData.encode(proto.MatchMetaData.create({ match_details: contents })).finish());
  assert.strictEqual(decodeMetadataFile(file).average_badge, 45);
});
