'use strict';
// Talks to the running fetcher (it holds the Steam session; a second login would kick it).
//   docker compose exec fetcher node src/cli.js health | history <steamid3> | match <match_id>
//   docker compose exec fetcher node src/cli.js probe <steamid3> [gap_s] [--target] [--alternate]
//                                                      [--cycles N] [--retry S, default 300]   (see src/probe.js)
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
    const opt = (name, fallback) => {
      const i = rest.indexOf(`--${name}`);
      return i >= 0 && /^\d+(\.\d+)?$/.test(rest[i + 1] || '') ? Number(rest[i + 1]) : fallback;
    };
    const gapS = /^\d+(\.\d+)?$/.test(rest[0] || '') ? Number(rest[0]) : 0;
    await probe({
      get, accountId: Number(arg), gapS, target: rest.includes('--target'), alternate: rest.includes('--alternate'),
      cycles: opt('cycles', 1), retryS: opt('retry', 30), log: (line) => console.log(`${new Date().toISOString()} ${line}`),
    });
    process.exit(0);
  }
  const paths = { health: '/health', history: `/history/${arg}`, match: `/matches/${arg}` };
  if (!paths[cmd] || (cmd !== 'health' && !/^\d+$/.test(arg || ''))) {
    console.error('usage: node src/cli.js health | history <steamid3> | match <match_id> | probe <steamid3> [gap_s] [--target] [--alternate] [--cycles N] [--retry S]');
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
  console.error(err.message);
  process.exit(1);
});
