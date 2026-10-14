'use strict';
// The accounts whose friend requests the bot accepts: the players the site tracks. The web app
// sends the list (PUT /friends) whenever it changes; it is kept on disk so a restart doesn't
// forget it before the web app sends it again.
const fs = require('node:fs');

const MAX_ACCOUNT_ID = 2 ** 32 - 1;

class FriendList {
  constructor(file) {
    this.file = file;
    this.ids = new Set();
    try {
      this.ids = new Set(JSON.parse(fs.readFileSync(file, 'utf8')));
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }
  }

  has(accountId) {
    return this.ids.has(accountId);
  }

  /** Replace the list. Throws TypeError on anything but an array of SteamID3s. */
  replace(accountIds) {
    if (!Array.isArray(accountIds) || !accountIds.every((id) => Number.isInteger(id) && id > 0 && id <= MAX_ACCOUNT_ID)) {
      throw new TypeError('account_ids must be an array of SteamID3s');
    }
    const sorted = [...new Set(accountIds)].sort((a, b) => a - b);
    fs.writeFileSync(`${this.file}.tmp`, JSON.stringify(sorted));
    fs.renameSync(`${this.file}.tmp`, this.file);
    this.ids = new Set(sorted);
    return sorted;
  }
}

module.exports = { FriendList };
