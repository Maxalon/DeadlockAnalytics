'use strict';
// Session with Deadlock's Game Coordinator (GC) through a logged-in Steam bot account.
//
// Handshake (same as the official client): log in, report "playing" Deadlock, then send a
// ClientHello every few seconds until the GC answers with ClientWelcome. Requests are
// GC "jobs": steam-user routes each response back to the callback of its request.
const { EventEmitter } = require('node:events');
const SteamUser = require('steam-user');
const SteamID = require('steamid');
const proto = require('./proto');

const APP_ID = 1422450;
const MSG = { WELCOME: 4004, HELLO: 4006, CONNECTION_STATUS: 4009, HISTORY: 9112, METADATA: 9167, UPDATE_BUILD: 9193 };
const HAVE_SESSION = 0; // GCConnectionStatus
const HELLO_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 20000;
const READY_TIMEOUT_MS = 60000;
// Space requests out; we only ever need a handful, and Steam rate-limits per account.
const MIN_REQUEST_GAP_MS = 1500;
const MAX_HISTORY_PAGES = 100;
// What the game client sends (see proto/deadlock.proto).
const HELLO = { client_session_need: 104, client_launcher: 0, engine: 1 };
// After Steam rate-limits match metadata, don't ask again before this; doubled each time the
// first request after a pause is refused too. Refused requests may count against the limit,
// so retrying sooner can keep the block going.
const METADATA_PAUSE_MS = 35 * 60 * 1000;
const MAX_METADATA_PAUSE_MS = 4 * 3600 * 1000;
// Thrown out because the account logged on (or started a game) elsewhere: log back on after
// this, doubled while it keeps happening.
const RELOGIN_DELAY_MS = 10 * 60 * 1000;
const MAX_RELOGIN_DELAY_MS = 2 * 3600 * 1000;
const KICKED = [SteamUser.EResult.LoggedInElsewhere, SteamUser.EResult.LogonSessionReplaced];
// Errors only a new `npm run login` (or Steam support) fixes: the saved login is no good.
const E = SteamUser.EResult;
const NEEDS_LOGIN = [
  E.InvalidPassword, E.AccessDenied, E.Expired, E.Revoked, E.AccountDisabled, E.AccountLogonDenied,
  E.InvalidLoginAuthCode, E.AccountLoginDeniedNeedTwoFactor, E.TwoFactorCodeMismatch, E.InvalidSignature,
  E.AccountLockedDown, E.Banned,
];
// Anything else (Steam unreachable, timed out, busy) is retried by logging on again after this,
// doubled while it keeps failing.
const RETRY_DELAY_MS = 60 * 1000;
const MAX_RETRY_DELAY_MS = 30 * 60 * 1000;
// Watchdog: without a GC session for this long, for no reason we're waiting out on purpose
// (login needed, kicked, someone playing on the account), the session is wedged.
const MAX_NOT_READY_MS = 15 * 60 * 1000;
// Steam checks in a row that may fail before the connection counts as dead.
const MAX_PROBE_FAILURES = 2;
const PROBE_TIMEOUT_MS = 30 * 1000;

const BUILD_RESPONSES = {
  2: ['conflict', 'the build belongs to another account'],
  3: ['bad_request', 'unknown build id'],
  4: ['bad_request', 'the game refused the build name'],
  5: ['bad_request', 'the build name is too long'],
  6: ['bad_request', 'the description is too long'],
  7: ['bad_request', 'the game refused the build contents'],
  8: ['conflict', 'the build changed in the game since; publish it as a new build'],
  9: ['bad_request', 'the build is too large'],
};

function avatarUrl(user) {
  const hash = Buffer.isBuffer(user.avatar_hash) ? user.avatar_hash.toString('hex') : '';
  if (!hash || /^0+$/.test(hash)) return null; // Steam's default avatar
  return `https://avatars.steamstatic.com/${hash}_medium.jpg`;
}

class GcError extends Error {
  constructor(code, message) {
    super(message || code);
    // not_ready | timeout | private | rate_limited | quota | invalid_match | in_flight | gc_error | bad_request | conflict
    this.code = code;
  }
}

class DeadlockGC extends EventEmitter {
  /**
   * @param {object} opts
   * @param {SteamUser} [opts.client] injectable for tests
   * @param {string} opts.refreshToken from `npm run login`
   * @param {(token: string) => void} opts.saveRefreshToken called when Steam renews the token
   * @param {() => number} [opts.now] injectable clock for tests
   * @param {number} [opts.reloginDelayMs] first wait before logging back on after being kicked
   * @param {number} [opts.retryDelayMs] first wait before logging on again after Steam couldn't be reached
   * @param {import('./quota').RollingQuota} [opts.saltsQuota] daily budget for match salts
   * @param {import('./quota').RollingQuota} [opts.historyQuota] daily budget for match history pages
   * @param {import('./quota').RollingQuota} [opts.buildsQuota] daily budget for publishing hero builds
   */
  constructor({
    client, refreshToken, saveRefreshToken, log = console, now = Date.now, reloginDelayMs = RELOGIN_DELAY_MS,
    retryDelayMs = RETRY_DELAY_MS,
    saltsQuota = null, historyQuota = null, buildsQuota = null,
  }) {
    super();
    this.client = client || new SteamUser({ renewRefreshTokens: true, autoRelogin: true });
    this.refreshToken = refreshToken;
    this.saveRefreshToken = saveRefreshToken;
    this.log = log;
    this.loggedIn = false;
    this.ready = false;
    this.lastError = null;
    this._helloTimer = null;
    this._queue = Promise.resolve();
    this._lastRequestAt = 0;
    this.now = now;
    this.saltsQuota = saltsQuota;
    this.historyQuota = historyQuota;
    this.buildsQuota = buildsQuota;
    this.playingElsewhere = false;
    this._metadataPausedUntil = 0;
    this._metadataPause = METADATA_PAUSE_MS;
    this._reloginDelay = reloginDelayMs;
    this._firstReloginDelay = reloginDelayMs;
    this._retryDelay = retryDelayMs;
    this._firstRetryDelay = retryDelayMs;
    this._reloginTimer = null;
    this.notReadySince = now();
    this._hold = null; // 'needs_login' | 'kicked': waiting on purpose, not stuck
    this._probeFailures = 0;
    this._wire();
  }

  start() {
    this.client.logOn({ refreshToken: this.refreshToken });
  }

  stop() {
    clearInterval(this._helloTimer);
    clearTimeout(this._reloginTimer);
    this.client.logOff();
  }

  status() {
    return {
      logged_in: this.loggedIn,
      gc_ready: this.ready,
      account_id: this.client.steamID ? this.client.steamID.accountid : null,
      playing_elsewhere: this.playingElsewhere,
      metadata_paused_until: this._metadataPausedUntil > this.now() ? new Date(this._metadataPausedUntil).toISOString() : null,
      salts_budget: this.saltsQuota ? this.saltsQuota.status() : null,
      history_budget: this.historyQuota ? this.historyQuota.status() : null,
      builds_budget: this.buildsQuota ? this.buildsQuota.status() : null,
      not_ready_since: this.notReadySince ? new Date(this.notReadySince).toISOString() : null,
      last_error: this.lastError,
    };
  }

  _wire() {
    const c = this.client;
    c.on('loggedOn', () => {
      this.loggedIn = true;
      this.lastError = null;
      this._hold = null;
      this._probeFailures = 0;
      this.notReadySince = this.now(); // the clock for a stuck session starts now
      this._reloginDelay = this._firstReloginDelay;
      this._retryDelay = this._firstRetryDelay;
      this.log.info(`steam: logged on as ${c.steamID && c.steamID.accountid}`);
      c.setPersona(SteamUser.EPersonaState.Online);
      c.gamesPlayed([APP_ID]);
      this._startHello();
    });
    c.on('refreshToken', (token) => {
      this.refreshToken = token;
      this.saveRefreshToken(token);
    });
    c.on('receivedFromGC', (appid, msgType, payload) => {
      if (appid === APP_ID && msgType === MSG.WELCOME && !this.ready) {
        this.ready = true;
        this.notReadySince = null;
        clearInterval(this._helloTimer);
        this.log.info('gc: welcome received, ready');
        this.emit('ready');
      }
      if (appid === APP_ID && msgType === MSG.CONNECTION_STATUS) this._connectionStatus(payload);
    });
    // Someone plays a game on the bot account elsewhere (e.g. its Steam client on a PC): that
    // ends our GC session. Never kick them; take Deadlock back once they've stopped.
    c.on('playingState', (blocked, playingApp) => {
      if (blocked && !this.playingElsewhere) {
        this.playingElsewhere = true;
        this._notReady();
        this.lastError = `the bot account is playing app ${playingApp} on another device; paused until that ends`;
        this.log.warn(`steam: ${this.lastError}`);
      } else if (!blocked && this.playingElsewhere) {
        this.playingElsewhere = false;
        this.lastError = null;
        this.notReadySince = this.now();
        this.log.info('steam: the other session stopped playing; resuming');
        c.gamesPlayed([APP_ID]);
        this._startHello();
      }
    });
    c.on('disconnected', (eresult, msg) => {
      this.loggedIn = false;
      this._notReady();
      this.log.warn(`steam: disconnected (${msg || eresult}); steam-user reconnects`);
    });
    c.on('error', (err) => {
      this.loggedIn = false;
      this._notReady();
      if (KICKED.includes(err.eresult)) {
        // The account logged on elsewhere. Come back later instead of fighting over it.
        const delay = this._reloginDelay;
        this._reloginDelay = Math.min(delay * 2, MAX_RELOGIN_DELAY_MS);
        this._hold = 'kicked';
        this.lastError = `logged on elsewhere (${err.message}); logging back on in ${Math.round(delay / 60000)} min`;
        this.log.warn(`steam: ${this.lastError}`);
        clearTimeout(this._reloginTimer);
        this._reloginTimer = setTimeout(() => this.start(), delay);
        return;
      }
      if (NEEDS_LOGIN.includes(err.eresult)) {
        // The saved login is no good (e.g. the refresh token was revoked): needs `npm run login` again.
        this._hold = 'needs_login';
        this.lastError = `${err.message}; log the bot in again (npm run login)`;
        this.log.error(`steam: ${this.lastError}`);
        return;
      }
      // Steam couldn't be reached or didn't answer (steam-user has given up retrying by itself).
      const delay = this._retryDelay;
      this._retryDelay = Math.min(delay * 2, MAX_RETRY_DELAY_MS);
      this.lastError = `${err.message}; trying again in ${Math.max(1, Math.round(delay / 60000))} min`;
      this.log.warn(`steam: ${this.lastError}`);
      clearTimeout(this._reloginTimer);
      this._reloginTimer = setTimeout(() => this.start(), delay);
    });
  }

  /**
   * The bot's friends list. Friend requests are never acted on automatically: they wait here
   * until someone accepts, declines or blocks them on the website.
   */
  friends() {
    const R = SteamUser.EFriendRelationship;
    const groups = { incoming: [], friends: [], blocked: [] };
    const group = { [R.RequestRecipient]: 'incoming', [R.Friend]: 'friends', [R.Blocked]: 'blocked', [R.IgnoredFriend]: 'blocked' };
    for (const [id64, relationship] of Object.entries(this.client.myFriends || {})) {
      if (!(relationship in group)) continue;
      const persona = (this.client.users || {})[id64];
      groups[group[relationship]].push({ account_id: new SteamID(id64).accountid, name: (persona && persona.player_name) || null });
    }
    for (const list of Object.values(groups)) list.sort((a, b) => a.account_id - b.account_id);
    return { logged_in: this.loggedIn, ...groups };
  }

  /**
   * accept / decline an incoming request, remove a friend, block or unblock. Each only applies
   * to an account in the matching state: in particular `accept` never sends a new invite.
   */
  async friendAction(accountId, action) {
    if (!this.loggedIn) throw new GcError('not_ready', 'the bot is not logged in to Steam');
    if (!Number.isInteger(accountId) || accountId < 1 || accountId > 2 ** 32 - 1) {
      throw new GcError('bad_request', `not a SteamID3: ${accountId}`);
    }
    const R = SteamUser.EFriendRelationship;
    const steamID = SteamID.fromIndividualAccountID(accountId);
    const relationship = (this.client.myFriends || {})[steamID.getSteamID64()];
    const allowed = {
      accept: [R.RequestRecipient],
      decline: [R.RequestRecipient],
      remove: [R.Friend],
      block: [R.RequestRecipient, R.Friend, R.None, undefined],
      unblock: [R.Blocked, R.IgnoredFriend],
    }[action];
    if (!allowed) throw new GcError('bad_request', `unknown action ${action}`);
    if (!allowed.includes(relationship)) throw new GcError('conflict', `can't ${action} ${accountId} in its current state`);
    if (action === 'accept') await this.client.addFriend(steamID);
    else if (action === 'decline' || action === 'remove') this.client.removeFriend(steamID);
    else if (action === 'block') await this.client.blockUser(steamID);
    else await this.client.unblockUser(steamID);
    this.log.info(`steam: ${action} ${accountId}`);
  }

  /**
   * Steam names and avatars of any accounts, friends or not. Asked over the bot's ordinary
   * Steam connection, not Deadlock's Game Coordinator, so it costs none of the daily budgets.
   * Accounts Steam doesn't answer for in time are left out.
   */
  async profiles(accountIds) {
    if (!this.loggedIn) throw new GcError('not_ready', 'the bot is not logged in to Steam');
    const ids = accountIds.map((a) => SteamID.fromIndividualAccountID(a).getSteamID64());
    try {
      await this.client.getPersonas(ids);
    } catch (err) {
      // Times out as a whole if one account never answers; the others are in `users` anyway.
      this.log.info(`steam: profile lookup incomplete (${err.message})`);
    }
    const users = this.client.users || {};
    return accountIds.flatMap((accountId, i) => {
      const u = users[ids[i]];
      return u && u.player_name ? [{ account_id: accountId, name: u.player_name, avatar_url: avatarUrl(u) }] : [];
    });
  }

  /** Decline every open friend request at once. */
  declineAll() {
    const incoming = this.friends().incoming;
    for (const { account_id: accountId } of incoming) this.client.removeFriend(SteamID.fromIndividualAccountID(accountId));
    return incoming.length;
  }

  /**
   * The GC says our session changed: it restarts on game updates (GC_GOING_DOWN) and can drop
   * sessions. Say hello again until it welcomes us back, as the game does.
   */
  _connectionStatus(payload) {
    let status;
    try {
      status = proto.ConnectionStatus.decode(payload || Buffer.alloc(0)).status || HAVE_SESSION;
    } catch {
      return;
    }
    if (status === HAVE_SESSION) return;
    this.log.warn(`gc: connection status ${status} (no session); saying hello again`);
    if (!this.loggedIn || this.playingElsewhere) {
      this._notReady();
      return;
    }
    this.client.gamesPlayed([APP_ID]);
    this._startHello();
  }

  /**
   * Ask Steam itself (not the GC) something cheap. A dead connection that steam-user hasn't
   * noticed shows up here: the bot looks logged on but nothing reaches it. Resolves to whether
   * Steam answered; skipped (true) while not logged on.
   */
  async probeSteam() {
    if (!this.loggedIn || !this.client.steamID) return true;
    let timer;
    try {
      await Promise.race([
        this.client.getPersonas([this.client.steamID]),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timed out')), PROBE_TIMEOUT_MS); }),
      ]);
      this._probeFailures = 0;
      return true;
    } catch (err) {
      this._probeFailures += 1;
      this.log.warn(`steam: liveness check failed (${err.message}), ${this._probeFailures} in a row`);
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Why the session looks wedged (a restart should fix it), or null if it is fine or we are
   * waiting on purpose: a new login is needed, we were kicked and come back later, or someone
   * plays on the account.
   */
  stuckReason(maxNotReadyMs = MAX_NOT_READY_MS) {
    if (this._probeFailures >= MAX_PROBE_FAILURES) return `Steam stopped answering (${this._probeFailures} liveness checks failed)`;
    if (this.ready || this._hold || this.playingElsewhere || this.notReadySince === null) return null;
    const down = this.now() - this.notReadySince;
    if (down < maxNotReadyMs) return null;
    const why = this.lastError || (this.loggedIn ? 'the GC never welcomed us' : 'not logged on to Steam');
    return `no GC session for ${Math.round(down / 60000)} min (${why})`;
  }

  _startHello() {
    this._notReady();
    const payload = Buffer.from(proto.ClientHello.encode(proto.ClientHello.create(HELLO)).finish());
    const hello = () => this.client.sendToGC(APP_ID, MSG.HELLO, {}, payload);
    hello();
    this._helloTimer = setInterval(hello, HELLO_INTERVAL_MS);
  }

  _notReady() {
    if (this.ready || this.notReadySince === null) this.notReadySince = this.now();
    this.ready = false;
    clearInterval(this._helloTimer);
  }

  _waitReady() {
    if (this.ready) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.off('ready', onReady);
        reject(new GcError('not_ready', 'Deadlock GC session is not ready (see /health)'));
      }, READY_TIMEOUT_MS);
      const onReady = () => {
        clearTimeout(timer);
        resolve();
      };
      this.once('ready', onReady);
    });
  }

  /** Send one GC job and decode its response. Jobs run one at a time, spaced out. */
  _request(msgType, ReqType, body, RespType) {
    const run = async () => {
      await this._waitReady();
      const wait = this._lastRequestAt + MIN_REQUEST_GAP_MS - Date.now();
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      this._lastRequestAt = Date.now();
      const payload = Buffer.from(ReqType.encode(ReqType.create(body)).finish());
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          // A silent GC usually means the session went stale: redo the handshake.
          this._startHello();
          reject(new GcError('timeout', `GC did not answer message ${msgType}`));
        }, REQUEST_TIMEOUT_MS);
        this.client.sendToGC(APP_ID, msgType, {}, payload, (_appid, _type, response) => {
          clearTimeout(timer);
          resolve(proto.toObject(RespType, RespType.decode(response)));
        });
      });
    };
    const result = this._queue.then(run, run);
    this._queue = result.catch(() => {});
    return result;
  }

  /** One page of `accountId`'s match history (newest first), older than `cursor` if given. */
  async historyPage(accountId, cursor) {
    if (this.historyQuota && !this.historyQuota.take()) {
      const next = this.historyQuota.status().next_free_at;
      throw new GcError('quota', `the bot's daily budget for match history is used up; next one free at ${next}`);
    }
    const body = { account_id: accountId };
    if (cursor) body.continue_cursor = cursor;
    const resp = await this._request(MSG.HISTORY, proto.GetMatchHistory, body, proto.GetMatchHistoryResponse);
    if (resp.result === 1) return resp;
    if (resp.result === 2) throw new GcError('private', `match history of ${accountId} is not visible to the bot (not friends?)`);
    if (resp.result === 5) throw new GcError('rate_limited', 'Steam rate-limited match history requests');
    throw new GcError('gc_error', `match history result ${resp.result}`);
  }

  /** The whole history, following the cursor. `complete` is false if Steam stopped us early. */
  async fullHistory(accountId, maxPages = MAX_HISTORY_PAGES) {
    const matches = [];
    let cursor = null;
    for (let page = 0; page < maxPages; page++) {
      let resp;
      try {
        resp = await this.historyPage(accountId, cursor);
      } catch (err) {
        if ((err.code === 'rate_limited' || err.code === 'quota') && matches.length) return { matches, complete: false };
        throw err;
      }
      matches.push(...resp.matches);
      const next = resp.continue_cursor && resp.continue_cursor !== '0' ? resp.continue_cursor : null;
      if (!resp.matches.length || !next || (cursor && BigInt(next) >= BigInt(cursor))) {
        return { matches, complete: true };
      }
      cursor = next;
    }
    return { matches, complete: false };
  }

  /**
   * The salts needed to download a match's files from Valve's replay servers. Like the game,
   * pass the account whose history the match is from.
   */
  async matchSalts(matchId, targetAccountId = null, { force = false } = {}) {
    // `force` (the probe only): ask Steam even during a pause, to measure the block.
    if (!force && this.now() < this._metadataPausedUntil) {
      throw new GcError('rate_limited', `paused after a Steam rate limit until ${new Date(this._metadataPausedUntil).toISOString()}`);
    }
    if (this.saltsQuota && !this.saltsQuota.take() && !force) {
      const next = this.saltsQuota.status().next_free_at;
      throw new GcError('quota', `the bot's daily budget for match salts is used up; next one free at ${next}`);
    }
    const body = { match_id: matchId };
    if (targetAccountId) body.target_account_id = targetAccountId;
    const resp = await this._request(MSG.METADATA, proto.GetMatchMetaData, body, proto.GetMatchMetaDataResponse);
    if (resp.result === 5) {
      this._metadataPausedUntil = this.now() + this._metadataPause;
      this.log.warn(`steam: match metadata rate-limited; pausing ${Math.round(this._metadataPause / 60000)} min, until ${new Date(this._metadataPausedUntil).toISOString()}`);
      this._metadataPause = Math.min(this._metadataPause * 2, MAX_METADATA_PAUSE_MS);
      throw new GcError('rate_limited', `Steam rate-limited match metadata requests; paused until ${new Date(this._metadataPausedUntil).toISOString()}`);
    }
    this._metadataPause = METADATA_PAUSE_MS; // Steam answered: the next limit starts with a short pause again
    if (resp.result === 1 && resp.metadata_salt) {
      return { cluster_id: resp.replay_group_id, metadata_salt: resp.metadata_salt, replay_salt: resp.replay_salt };
    }
    if (resp.result === 6) throw new GcError('invalid_match', `match ${matchId} is unknown to Valve`);
    if (resp.result === 7) throw new GcError('in_flight', `match ${matchId} is still being processed`);
    throw new GcError('gc_error', `match metadata result ${resp.result}${resp.result === 1 ? ' without salt' : ''}`);
  }

  /**
   * Publish a hero build under the bot's account: a new one when `hero_build_id` is 0 or missing,
   * else an update of the bot's own build at `version`. Returns { hero_build_id, version }.
   */
  async publishBuild(build) {
    if (this.buildsQuota && !this.buildsQuota.take()) {
      throw new GcError('quota', `the bot's daily budget for publishing builds is used up; next one free at ${this.buildsQuota.status().next_free_at}`);
    }
    const accountId = this.client.steamID ? this.client.steamID.accountid : 0;
    const now = Math.floor(this.now() / 1000);
    const heroBuild = {
      ...build,
      hero_build_id: build.hero_build_id || 0,
      author_account_id: accountId,
      last_updated_timestamp: now,
      publish_timestamp: now,
      language: build.language || 0,
    };
    const resp = await this._request(MSG.UPDATE_BUILD, proto.UpdateHeroBuild, { hero_build: heroBuild }, proto.UpdateHeroBuildResponse);
    if (resp.response === 1) return { hero_build_id: resp.hero_build_id, version: resp.version };
    const [code, message] = BUILD_RESPONSES[resp.response] || ['gc_error', `the game didn't accept the build (response ${resp.response})`];
    throw new GcError(code, message);
  }
}

module.exports = { DeadlockGC, GcError, APP_ID, MSG, MAX_NOT_READY_MS };
