'use strict';
// Internal HTTP service for the web app. Not exposed outside the compose network.
const fs = require('node:fs');
const http = require('node:http');
const config = require('./config');
const { DeadlockGC } = require('./gc');
const { RollingQuota } = require('./quota');
const { Service } = require('./service');

const log = {
  info: (...a) => console.log(new Date().toISOString(), 'INFO', ...a),
  warn: (...a) => console.warn(new Date().toISOString(), 'WARN', ...a),
  error: (...a) => console.error(new Date().toISOString(), 'ERROR', ...a),
};

function readToken() {
  try {
    return fs.readFileSync(config.refreshTokenFile, 'utf8').trim();
  } catch {
    return null;
  }
}

function saveToken(token) {
  fs.writeFileSync(config.refreshTokenFile, token, { mode: 0o600 });
  log.info('steam: refresh token renewed and saved');
}

const token = readToken();
if (!token) {
  log.error(`no Steam login yet: run \`docker compose run --rm -it fetcher npm run login\` first (${config.refreshTokenFile})`);
}
const gc = new DeadlockGC({
  refreshToken: token, saveRefreshToken: saveToken, log,
  saltsQuota: new RollingQuota({ file: config.saltsQuotaFile, limit: config.saltsPerDay }),
  historyQuota: new RollingQuota({ file: config.historyQuotaFile, limit: config.historyPagesPerDay }),
});
const service = new Service({
  gc, cacheDir: config.matchCacheDir, replayUrl: config.replayUrl, saltsApi: config.saltsApi, apiKey: config.apiKey,
  contributeSalts: config.contributeSalts, log,
});
if (token) {
  gc.start();
  // Watchdog: a wedged session (GC never came back after an update, Steam connection dead
  // without steam-user noticing) is fixed by a fresh start, so exit and let Docker's restart
  // policy start us again. Waits that a restart can't fix (login needed, someone playing on
  // the account, kicked) don't count.
  let ticks = 0;
  setInterval(async () => {
    if (++ticks % config.probeEveryMinutes === 0) await gc.probeSteam();
    const reason = gc.stuckReason(config.maxNotReadyMinutes * 60000);
    if (reason) {
      log.error(`watchdog: ${reason}; exiting so the container restarts`);
      gc.stop();
      setTimeout(() => process.exit(1), 2000);
    }
  }, 60000).unref();
}

http.createServer(async (req, res) => {
  const { pathname, searchParams } = new URL(req.url, 'http://localhost');
  const { status, body } = await service.handle(pathname, req.method, searchParams);
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}).listen(config.port, () => log.info(`fetcher listening on :${config.port}`));

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    gc.stop();
    process.exit(0);
  });
}
