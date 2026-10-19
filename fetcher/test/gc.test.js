'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { EventEmitter } = require('node:events');
const SteamID = require('steamid');
const SteamUser = require('steam-user');
const proto = require('../src/proto');
const { DeadlockGC, APP_ID, MSG } = require('../src/gc');
const { RollingQuota } = require('../src/quota');

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
  logOn() { this.logons = (this.logons || 0) + 1; this.steamID = new SteamID('76561199000000001'); setImmediate(() => this.emit('loggedOn')); }
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

function session(client, opts = {}) {
  const gc = new DeadlockGC({ client, refreshToken: 't', saveRefreshToken() {}, log: quiet, ...opts });
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

test('match salts request names the account whose history it is from, like the game', async () => {
  const seen = [];
  const client = new FakeSteam({
    onMetadata: (req) => { seen.push(req.target_account_id); return { result: 1, metadata_salt: 1, replay_group_id: 1 }; },
  });
  const gc = session(client);
  gc.start();
  await gc.matchSalts(1, 113956776);
  await gc.matchSalts(1);
  assert.deepStrictEqual(seen, [113956776, 0]);
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

test('profiles of any accounts, over Steam itself rather than the GC; slow ones are left out', async () => {
  const { client, gc } = await loggedIn({});
  const asked = [];
  client.getPersonas = async (ids) => {
    asked.push(ids);
    client.users[id64(7)] = { player_name: 'seven', avatar_hash: Buffer.from('ab'.repeat(20), 'hex') };
    client.users[id64(8)] = { player_name: 'eight', avatar_hash: Buffer.alloc(20) };
    throw new Error('Request timed out'); // account 9 never answered
  };
  const sentBefore = client.sent.length;
  assert.deepStrictEqual(await gc.profiles([7, 8, 9]), [
    { account_id: 7, name: 'seven', avatar_url: `https://avatars.steamstatic.com/${'ab'.repeat(20)}_medium.jpg` },
    { account_id: 8, name: 'eight', avatar_url: null },
  ]);
  assert.deepStrictEqual(asked, [[id64(7), id64(8), id64(9)]]);
  assert.strictEqual(client.sent.length, sentBefore); // nothing went to the GC
  gc.stop();
});

test('friend actions need a Steam login', async () => {
  const gc = session(new FakeSteam());
  await assert.rejects(gc.friendAction(42, 'accept'), { code: 'not_ready' });
});

test('after a rate limit the bot pauses instead of asking Steam again, and backs off further', async () => {
  let t = 0;
  let answer = 5;
  const client = new FakeSteam({ onMetadata: () => ({ result: answer, metadata_salt: 1, replay_group_id: 1 }) });
  const gc = session(client, { now: () => t });
  gc.start();
  await new Promise((r) => gc.once('ready', r));
  const asked = () => client.sent.filter((m) => m === MSG.METADATA).length;

  await assert.rejects(gc.matchSalts(1), { code: 'rate_limited' });
  assert.strictEqual(asked(), 1);
  t += 30 * 60 * 1000; // still within the 35 min pause: Steam isn't asked
  await assert.rejects(gc.matchSalts(1), /paused/);
  assert.strictEqual(asked(), 1);
  assert.ok(gc.status().metadata_paused_until);

  t += 6 * 60 * 1000; // pause over, but Steam still refuses: the next pause is 70 min
  await assert.rejects(gc.matchSalts(1), { code: 'rate_limited' });
  t += 60 * 60 * 1000;
  await assert.rejects(gc.matchSalts(1), /paused/);
  assert.strictEqual(asked(), 2);

  // the probe may ask anyway; a success resets the backoff
  answer = 1;
  assert.deepStrictEqual(await gc.matchSalts(1, null, { force: true }), { cluster_id: 1, metadata_salt: 1, replay_salt: 0 });
  answer = 5;
  t += 11 * 60 * 1000;
  await assert.rejects(gc.matchSalts(1), { code: 'rate_limited' });
  t += 36 * 60 * 1000; // back to a 35 min pause
  answer = 1;
  await gc.matchSalts(1);
  gc.stop();
});

test('a game started on the bot account elsewhere pauses the bot until it ends, without kicking it', async () => {
  const client = new FakeSteam();
  const gc = session(client);
  gc.start();
  await new Promise((r) => gc.once('ready', r));
  client.played = [];
  client.gamesPlayed = (apps) => client.played.push(apps);

  client.emit('playingState', true, APP_ID);
  assert.strictEqual(gc.status().gc_ready, false);
  assert.strictEqual(gc.status().playing_elsewhere, true);
  assert.match(gc.status().last_error, /another device/);
  assert.deepStrictEqual(client.played, []); // never takes the game back while it's in use

  client.emit('playingState', false, 0);
  await new Promise((r) => gc.once('ready', r));
  assert.deepStrictEqual(client.played, [[APP_ID]]);
  assert.strictEqual(gc.status().last_error, null);
  gc.stop();
});

test('thrown out by a logon elsewhere: logs back on later by itself', async () => {
  const client = new FakeSteam();
  const gc = session(client, { reloginDelayMs: 20 });
  gc.start();
  await new Promise((r) => gc.once('ready', r));
  const kicked = Object.assign(new Error('LoggedInElsewhere'), { eresult: SteamUser.EResult.LoggedInElsewhere });
  client.emit('error', kicked);
  assert.strictEqual(gc.status().logged_in, false);
  assert.match(gc.status().last_error, /logging back on/);
  await new Promise((r) => gc.once('ready', r));
  assert.strictEqual(client.logons, 2);

  // any other fatal error (e.g. revoked login) is left for a human
  client.emit('error', Object.assign(new Error('AccessDenied'), { eresult: SteamUser.EResult.AccessDenied }));
  await new Promise((r) => setTimeout(r, 60));
  assert.strictEqual(client.logons, 2);
  gc.stop();
});

test('daily budgets: salts and history pages stop before Steam is asked once used up', async () => {
  const client = new FakeSteam({
    onMetadata: () => ({ result: 1, metadata_salt: 1, replay_group_id: 1 }),
    onHistory: () => ({ result: 1, matches: [{ match_id: 1 }], continue_cursor: 5 }),
  });
  const gc = session(client, {
    saltsQuota: new RollingQuota({ file: null, limit: 2 }),
    historyQuota: new RollingQuota({ file: null, limit: 1 }),
  });
  gc.start();
  await new Promise((r) => gc.once('ready', r));
  const sent = (m) => client.sent.filter((x) => x === m).length;
  await gc.matchSalts(1);
  await gc.matchSalts(2);
  await assert.rejects(gc.matchSalts(3), { code: 'quota' });
  assert.strictEqual(sent(MSG.METADATA), 2);
  assert.strictEqual(gc.status().salts_budget.remaining, 0);

  // history: the second page would exceed the budget, so the first page comes back as incomplete
  const h = await gc.fullHistory(7);
  assert.deepStrictEqual([h.matches.length, h.complete], [1, false]);
  await assert.rejects(gc.fullHistory(7), { code: 'quota' });
  assert.strictEqual(sent(MSG.HISTORY), 1);
  gc.stop();
});
