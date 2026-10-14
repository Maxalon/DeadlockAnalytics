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
const MSG = { WELCOME: 4004, HELLO: 4006, HISTORY: 9112, METADATA: 9167 };
const HELLO_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 20000;
const READY_TIMEOUT_MS = 60000;
// Space requests out; we only ever need a handful, and Steam rate-limits per account.
const MIN_REQUEST_GAP_MS = 1500;
const MAX_HISTORY_PAGES = 100;
// What the game client sends (see proto/deadlock.proto).
const HELLO = { client_session_need: 104, client_launcher: 0, engine: 1 };

class GcError extends Error {
  constructor(code, message) {
    super(message || code);
    this.code = code; // not_ready | timeout | private | rate_limited | invalid_match | in_flight | gc_error
  }
}

class DeadlockGC extends EventEmitter {
  /**
   * @param {object} opts
   * @param {SteamUser} [opts.client] injectable for tests
   * @param {string} opts.refreshToken from `npm run login`
   * @param {(token: string) => void} opts.saveRefreshToken called when Steam renews the token
   * @param {() => Set<number>} opts.allowedFriends SteamID3s whose friend requests are accepted
   */
  constructor({ client, refreshToken, saveRefreshToken, allowedFriends, log = console }) {
    super();
    this.client = client || new SteamUser({ renewRefreshTokens: true, autoRelogin: true });
    this.refreshToken = refreshToken;
    this.saveRefreshToken = saveRefreshToken;
    this.allowedFriends = allowedFriends;
    this.log = log;
    this.loggedIn = false;
    this.ready = false;
    this.lastError = null;
    this._helloTimer = null;
    this._queue = Promise.resolve();
    this._lastRequestAt = 0;
    this._wire();
  }

  start() {
    this.client.logOn({ refreshToken: this.refreshToken });
  }

  stop() {
    clearInterval(this._helloTimer);
    this.client.logOff();
  }

  status() {
    return {
      logged_in: this.loggedIn,
      gc_ready: this.ready,
      account_id: this.client.steamID ? this.client.steamID.accountid : null,
      last_error: this.lastError,
    };
  }

  _wire() {
    const c = this.client;
    c.on('loggedOn', () => {
      this.loggedIn = true;
      this.lastError = null;
      this.log.info(`steam: logged on as ${c.steamID && c.steamID.accountid}`);
      c.setPersona(SteamUser.EPersonaState.Online);
      c.gamesPlayed([APP_ID]);
      this._startHello();
    });
    c.on('refreshToken', (token) => {
      this.refreshToken = token;
      this.saveRefreshToken(token);
    });
    c.on('receivedFromGC', (appid, msgType) => {
      if (appid === APP_ID && msgType === MSG.WELCOME && !this.ready) {
        this.ready = true;
        clearInterval(this._helloTimer);
        this.log.info('gc: welcome received, ready');
        this.emit('ready');
      }
    });
    c.on('friendRelationship', (steamID, relationship) => this._maybeAccept(steamID, relationship));
    c.on('friendsList', () => {
      // Requests that arrived while the bot was offline.
      for (const [id64, relationship] of Object.entries(c.myFriends || {})) {
        this._maybeAccept(new SteamID(id64), relationship);
      }
    });
    c.on('disconnected', (eresult, msg) => {
      this.loggedIn = false;
      this._notReady();
      this.log.warn(`steam: disconnected (${msg || eresult}); steam-user reconnects`);
    });
    c.on('error', (err) => {
      // Fatal for this login (e.g. the refresh token was revoked): needs `npm run login` again.
      this.loggedIn = false;
      this._notReady();
      this.lastError = err.message;
      this.log.error(`steam: ${err.message}`);
    });
  }

  _maybeAccept(steamID, relationship) {
    if (relationship !== SteamUser.EFriendRelationship.RequestRecipient) return;
    const accountId = steamID.accountid;
    if (this.allowedFriends().has(accountId)) {
      this.log.info(`steam: accepting friend request from ${accountId}`);
      this.client.addFriend(steamID);
    } else {
      this.log.info(`steam: ignoring friend request from ${accountId} (not in FRIEND_ACCOUNT_IDS)`);
    }
  }

  _startHello() {
    this._notReady();
    const payload = Buffer.from(proto.ClientHello.encode(proto.ClientHello.create(HELLO)).finish());
    const hello = () => this.client.sendToGC(APP_ID, MSG.HELLO, {}, payload);
    hello();
    this._helloTimer = setInterval(hello, HELLO_INTERVAL_MS);
  }

  _notReady() {
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
        if (err.code === 'rate_limited' && matches.length) return { matches, complete: false };
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

  /** The salts needed to download a match's files from Valve's replay servers. */
  async matchSalts(matchId) {
    const resp = await this._request(
      MSG.METADATA, proto.GetMatchMetaData, { match_id: matchId }, proto.GetMatchMetaDataResponse,
    );
    if (resp.result === 1 && resp.metadata_salt) {
      return { cluster_id: resp.replay_group_id, metadata_salt: resp.metadata_salt, replay_salt: resp.replay_salt };
    }
    if (resp.result === 5) throw new GcError('rate_limited', 'Steam rate-limited match metadata requests');
    if (resp.result === 6) throw new GcError('invalid_match', `match ${matchId} is unknown to Valve`);
    if (resp.result === 7) throw new GcError('in_flight', `match ${matchId} is still being processed`);
    throw new GcError('gc_error', `match metadata result ${resp.result}${resp.result === 1 ? ' without salt' : ''}`);
  }
}

module.exports = { DeadlockGC, GcError, APP_ID, MSG };
