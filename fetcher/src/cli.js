'use strict';
// Talks to the running fetcher (it holds the Steam session; a second login would kick it).
//   docker compose exec fetcher node src/cli.js health | history <steamid3> | match <match_id>
const config = require('./config');

async function main() {
  const [cmd, arg] = process.argv.slice(2);
  const paths = { health: '/health', history: `/history/${arg}`, match: `/matches/${arg}` };
  if (!paths[cmd] || (cmd !== 'health' && !/^\d+$/.test(arg || ''))) {
    console.error('usage: node src/cli.js health | history <steamid3> | match <match_id>');
    process.exit(2);
  }
  const resp = await fetch(`http://127.0.0.1:${config.port}${paths[cmd]}`);
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
