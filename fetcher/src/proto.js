'use strict';
// Message types for the subset of Deadlock GC messages in proto/deadlock.proto.
const path = require('node:path');
const protobuf = require('protobufjs');

// keepCase: field names stay as in Valve's definitions (match_details, not matchDetails).
const root = new protobuf.Root().loadSync(path.join(__dirname, '..', 'proto', 'deadlock.proto'), { keepCase: true });
const type = (name) => root.lookupType(name);

module.exports = {
  ClientHello: type('CMsgClientHello'),
  GetMatchHistory: type('CMsgClientToGCGetMatchHistory'),
  GetMatchHistoryResponse: type('CMsgClientToGCGetMatchHistoryResponse'),
  GetMatchMetaData: type('CMsgClientToGCGetMatchMetaData'),
  GetMatchMetaDataResponse: type('CMsgClientToGCGetMatchMetaDataResponse'),
  MatchMetaData: type('CMsgMatchMetaData'),
  MatchMetaDataContents: type('CMsgMatchMetaDataContents'),
  // Plain JS numbers/strings instead of Long objects; 64-bit ids become strings.
  toObject: (T, msg) => T.toObject(msg, { longs: String, defaults: true }),
};
