'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { EventEmitter } = require('node:events');
const SteamID = require('steamid');
const SteamUser = require('steam-user');
const proto = require('../src/proto');
const { DeadlockGC, APP_ID, MSG } = require('../src/gc');

const quiet = { info() {}, warn() {}, error() {} };
const enc = (T, body) => Buffer.from(T.encode(T.create(body)).finish());

/** Answers GC messages the way Steam does; `onHistory`/`onMetadata` build the responses. */
class FakeSteam extends EventEmitter {
  constructor({ onHistory, onMetadata } = {}) {
    super();
    this.onHistory = onHistory;
    this.onMetadata = onMetadata;
    this.sent = [];
    this.added = [];
    this.removed = [];
    this.blocked = [];
    this.unblocked = [];
    this.myFriends = {};
  }
  logOn() { this.steamID = new SteamID('76561199000000001'); setImmediate(() => this.emit('loggedOn')); }
  logOff() {}
  setPersona() {}
  gamesPlayed(apps) { this.playing = apps; }
  async addFriend(steamID) { this.added.push(steamID.accountid); }
  removeFriend(steamID) { this.removed.push(steamID.accountid); }
  async blockUser(steamID) { this.blocked.push(steamID.accountid); }
  async unblockUser(steamID) { this.unblocked.push(steamID.accountid); }
  sendToGC(appid, msgType, _hdr, payload, cb) {
    this.sent.push(msgType);
    // Like the real GC, only a Source 2 hello gets a welcome.
    if (msgType === MSG.HELLO && proto.ClientHello.decode(payload).engine === 1) {
      setImmediate(() => this.emit('receivedFromGC', APP_ID, MSG.WELCOME, Buffer.alloc(0)));
    }
    if (msgType === MSG.HISTORY) {
      const req = proto.toObject(proto.GetMatchHistory, proto.GetMatchHistory.decode(payload));
      setImmediate(() => cb(appid, msgType + 1, enc(proto.GetMatchHistoryResponse, this.onHistory(req))));
    }
    if (msgType === MSG.METADATA) {
      const req = proto.toObject(proto.GetMatchMetaData, proto.GetMatchMetaData.decode(payload));
      setImmediate(() => cb(appid, msgType + 1, enc(proto.GetMatchMetaDataResponse, this.onMetadata(req))));
    }
  }
}

function session(client) {
  const gc = new DeadlockGC({ client, refreshToken: 't', saveRefreshToken() {}, log: quiet });
  gc._lastRequestAt = -Infinity;
  return gc;
}

test('handshake: plays Deadlock and becomes ready on the welcome', async () => {
  const client = new FakeSteam();
  const gc = session(client);
  gc.start();
  await new Promise((r) => gc.once('ready', r));
  assert.deepStrictEqual(client.playing, [APP_ID]);
  assert.ok(client.sent.includes(MSG.HELLO));
  assert.strictEqual(gc.status().gc_ready, true);
  gc.stop();
});

test('full history follows the cursor until it runs out', async () => {
  const pages = { 0: [3, 2], 2: [1] };
  const client = new FakeSteam({
    onHistory: ({ account_id, continue_cursor }) => {
      assert.strictEqual(account_id, 42);
      const page = pages[Number(continue_cursor || 0)];
      return { result: 1, continue_cursor: page[0] === 1 ? 0 : 2, matches: page.map((id) => ({ match_id: id, hero_id: 3 })) };
    },
  });
  const gc = session(client);
  gc.start();
  const { matches, complete } = await gc.fullHistory(42);
  assert.deepStrictEqual(matches.map((m) => Number(m.match_id)), [3, 2, 1]);
  assert.strictEqual(complete, true);
  gc.stop();
});

test('private history and rate limits become typed errors', async () => {
  let result = 2;
  const client = new FakeSteam({ onHistory: () => ({ result }) });
  const gc = session(client);
  gc.start();
  await assert.rejects(gc.historyPage(7), { code: 'private' });
  result = 5;
  await assert.rejects(gc.historyPage(7), { code: 'rate_limited' });
  gc.stop();
});

test('match salts', async () => {
  const client = new FakeSteam({
    onMetadata: ({ match_id }) => (Number(match_id) === 1
      ? { result: 1, metadata_salt: 99, replay_salt: 5, replay_group_id: 3 }
      : { result: 6 }),
  });
  const gc = session(client);
  gc.start();
  assert.deepStrictEqual(await gc.matchSalts(1), { cluster_id: 3, metadata_salt: 99, replay_salt: 5 });
  await assert.rejects(gc.matchSalts(2), { code: 'invalid_match' });
  gc.stop();
});

const R = SteamUser.EFriendRelationship;
const id64 = (account) => SteamID.fromIndividualAccountID(account).getSteamID64();

async function loggedIn(myFriends) {
  const client = new FakeSteam();
  client.myFriends = myFriends;
  client.users = { [id64(42)]: { player_name: 'player42' } };
  const gc = session(client);
  gc.start();
  await new Promise((r) => gc.once('ready', r));
  return { client, gc };
}

test('friend requests are never accepted automatically', async () => {
  const { client, gc } = await loggedIn({ [id64(42)]: R.RequestRecipient });
  client.emit('friendRelationship', SteamID.fromIndividualAccountID(43), R.RequestRecipient);
  client.emit('friendsList');
  assert.deepStrictEqual(client.added, []);
  gc.stop();
});

test('friends list is grouped for the website', async () => {
  const { gc } = await loggedIn({
    [id64(42)]: R.RequestRecipient, [id64(7)]: R.Friend, [id64(9)]: R.Blocked, [id64(11)]: R.RequestInitiator,
  });
  assert.deepStrictEqual(gc.friends(), {
    logged_in: true,
    incoming: [{ account_id: 42, name: 'player42' }],
    friends: [{ account_id: 7, name: null }],
    blocked: [{ account_id: 9, name: null }],
  });
  gc.stop();
});

test('friend actions only apply to accounts in the matching state', async () => {
  const { client, gc } = await loggedIn({ [id64(42)]: R.RequestRecipient, [id64(7)]: R.Friend, [id64(9)]: R.Blocked });
  await gc.friendAction(42, 'accept');
  assert.deepStrictEqual(client.added, [42]);
  // accept never sends a new invite to someone who didn't ask
  await assert.rejects(gc.friendAction(5, 'accept'), { code: 'conflict' });
  await assert.rejects(gc.friendAction(7, 'decline'), { code: 'conflict' });
  await gc.friendAction(7, 'remove');
  await gc.friendAction(9, 'unblock');
  await gc.friendAction(5, 'block');
  assert.deepStrictEqual([client.added, client.removed, client.unblocked, client.blocked], [[42], [7], [9], [5]]);
  await assert.rejects(gc.friendAction(5, 'befriend'), { code: 'bad_request' });
  await assert.rejects(gc.friendAction(0, 'block'), { code: 'bad_request' });
  gc.stop();
});

test('decline all open requests', async () => {
  const { client, gc } = await loggedIn({ [id64(42)]: R.RequestRecipient, [id64(43)]: R.RequestRecipient, [id64(7)]: R.Friend });
  assert.strictEqual(gc.declineAll(), 2);
  assert.deepStrictEqual(client.removed, [42, 43]);
  gc.stop();
});

test('friend actions need a Steam login', async () => {
  const gc = session(new FakeSteam());
  await assert.rejects(gc.friendAction(42, 'accept'), { code: 'not_ready' });
});
