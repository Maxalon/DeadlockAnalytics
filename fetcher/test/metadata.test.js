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
      abandon_match_time_s: 0,
    },
  );
  assert.strictEqual(bySlot[12].account_id, 113956776);
  assert.strictEqual(bySlot[12].max_player_damage, 33681);
  assert.strictEqual(bySlot[12].max_boss_damage, 3244);
  assert.strictEqual(bySlot[12].mvp_rank, 2);
  assert.strictEqual(match.players.filter((p) => p.mvp_rank).length, 3);
});
