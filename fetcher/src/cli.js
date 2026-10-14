'use strict';
// Talks to the running fetcher (it holds the Steam session; a second login would kick it).
//   docker compose exec fetcher node src/cli.js health | history <steamid3> | match <match_id>
//   docker compose exec fetcher node src/cli.js probe <steamid3> [gap_seconds] [--target]
const config = require('./config');
const { probe } = require('./probe');

const base = `http://127.0.0.1:${config.port}`;

async function get(path) {
  const resp = await fetch(base + path);
  return { status: resp.status, body: await resp.json() };
}

async function main() {
  const [cmd, arg, ...rest] = process.argv.slice(2);
  if (cmd === 'probe' && /^\d+$/.test(arg || '')) {
    const gapS = Number(rest.find((a) => /^\d+(\.\d+)?$/.test(a)) ?? 0);
    const summary = await probe({ get, accountId: Number(arg), gapS, target: rest.includes('--target'), log: console.log });
    console.log(JSON.stringify(summary));
    process.exit(0);
  }
  const paths = { health: '/health', history: `/history/${arg}`, match: `/matches/${arg}` };
  if (!paths[cmd] || (cmd !== 'health' && !/^\d+$/.test(arg || ''))) {
    console.error('usage: node src/cli.js health | history <steamid3> | match <match_id> | probe <steamid3> [gap_s] [--target]');
    process.exit(2);
  }
  const resp = await fetch(base + paths[cmd]);
  const body = await resp.json();
  if (cmd === 'history' && resp.ok) {
    const oldest = body.matches.at(-1);
    console.log(`${body.matches.length} matches (complete: ${body.complete}); oldest:`, oldest && new Date(oldest.start_time * 1000).toISOString());
    console.log(JSON.stringify(body.matches.slice(0, 3), null, 2));
  } else if (cmd === 'match' && resp.ok) {
    console.log(`match ${body.match_id}: ${body.game_mode}/${body.match_mode}, ${body.duration_s}s, winner ${body.winning_team}`);
    for (const p of body.players) console.log(`  slot ${p.player_slot} ${p.team} hero ${p.hero_id} ${p.kills}/${p.deaths}/${p.assists} dmg ${p.max_player_damage} obj ${p.max_boss_damage} mvp ${p.mvp_rank ?? '-'}`);
  } else {
    console.log(resp.status, JSON.stringify(body, null, 2));
  }
  process.exit(resp.ok ? 0 : 1);
}

main().catch((err) => {
  console.error(`fetcher not reachable: ${err.message}`);
  process.exit(1);
});
