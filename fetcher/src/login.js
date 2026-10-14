'use strict';
// One-time interactive login for the bot account. Stores a Steam refresh token (no password)
// in the data volume; the server uses and renews it from then on.
//   docker compose run --rm -it fetcher npm run login
const fs = require('node:fs');
const readline = require('node:readline/promises');
const { Writable } = require('node:stream');
const SteamUser = require('steam-user');
const config = require('./config');

async function main() {
  let muted = false;
  const output = new Writable({ write: (chunk, _enc, cb) => { if (!muted) process.stdout.write(chunk); cb(); } });
  const rl = readline.createInterface({ input: process.stdin, output, terminal: true });
  const accountName = (await rl.question('Bot account name: ')).trim();
  process.stdout.write('Password (not shown, not stored): ');
  muted = true;
  const password = await rl.question('');
  muted = false;
  process.stdout.write('\n');

  const client = new SteamUser({ autoRelogin: false });
  client.on('steamGuard', async (domain, callback, lastCodeWrong) => {
    if (lastCodeWrong) console.log('That code was wrong, try again.');
    const where = domain ? `the email sent to ${domain}` : 'the Steam Guard mobile app';
    callback((await rl.question(`Steam Guard code from ${where}: `)).trim());
  });
  client.on('refreshToken', (token) => {
    fs.mkdirSync(config.dataDir, { recursive: true });
    fs.writeFileSync(config.refreshTokenFile, token, { mode: 0o600 });
    console.log(`Logged in. Token saved to ${config.refreshTokenFile}. Restart the fetcher: docker compose restart fetcher`);
    client.logOff();
    rl.close();
    setTimeout(() => process.exit(0), 500);
  });
  client.on('error', (err) => {
    console.error(`Login failed: ${err.message}`);
    rl.close();
    process.exit(1);
  });
  client.logOn({ accountName, password });
}

main();
