'use strict';
// Internal HTTP service for the web app. Not exposed outside the compose network.
const fs = require('node:fs');
const http = require('node:http');
const config = require('./config');
const { DeadlockGC } = require('./gc');
const { FriendList } = require('./friends');
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
const friends = new FriendList(config.friendsFile);
const gc = new DeadlockGC({ refreshToken: token, saveRefreshToken: saveToken, allowedFriends: (id) => friends.has(id), log });
const service = new Service({ gc, friends, cacheDir: config.matchCacheDir, replayUrl: config.replayUrl });
const MAX_BODY_BYTES = 64 * 1024;

async function readJson(req) {
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > MAX_BODY_BYTES) throw new SyntaxError('body too large');
  }
  return JSON.parse(raw);
}
if (token) gc.start();

http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, 'http://localhost');
  let result;
  try {
    const payload = req.method === 'PUT' ? await readJson(req) : undefined;
    result = await service.handle(pathname, req.method, payload);
  } catch (err) {
    result = { status: 400, body: { error: 'bad_request', message: err.message } };
  }
  const { status, body } = result;
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}).listen(config.port, () => log.info(`fetcher listening on :${config.port}`));

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    gc.stop();
    process.exit(0);
  });
}
