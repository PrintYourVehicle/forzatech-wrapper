// Offline, file-only FH6 GameDB decryption using statically decoded constants.
// This script never loads the downloaded runtime, launches the game, opens a
// process, or modifies any installed game file.
// Format reference: https://github.com/HTTPJXRDN/Forza-Mod-Tool/blob/main/GameDb.cs
// Scramble reference: https://github.com/HTTPJXRDN/Forza-Mod-Tool/blob/main/Scramble.cs
// Usage: node decrypt_fh6_gamedb.cjs input.slt static-key-candidates.json output.sqlite receipt.json
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {DatabaseSync} = require('node:sqlite');
const [inputPath, constantsPath, outputPath, receiptPath] = process.argv.slice(2);
assert(inputPath && constantsPath && outputPath && receiptPath, 'Four file paths are required.');
assert(!fs.existsSync(outputPath), 'Refusing to overwrite the output.');
assert(!fs.existsSync(receiptPath), 'Refusing to overwrite the receipt.');
const started = Date.now();
const sha256 = data => crypto.createHash('sha256').update(data).digest('hex');
const file = fs.readFileSync(inputPath);
const constants = JSON.parse(fs.readFileSync(constantsPath, 'utf8'));
const candidate = constants.keys.find(k => k.usage === 'GameDB');
assert(candidate, 'No GameDB key candidate.');
const key = Buffer.from(candidate.dataKey, 'hex');
const salt = Buffer.from(constants.transportKey1, 'hex');
const info = Buffer.from(constants.transportKey2, 'hex');
assert.equal(key.length, 32);
assert.equal(salt.length, 32);
assert.equal(info.length, 32);
const headerBytes = 36;
const chunkBytes = 131072;
const slotBytes = chunkBytes + 16;
assert(file.length > headerBytes && (file.length - headerBytes) % slotBytes === 0, 'Invalid GameDB slot framing.');
const slots = (file.length - headerBytes) / slotBytes;
const paddingBytes = file.readUInt32LE(16);
assert(paddingBytes < chunkBytes, 'Invalid padding length.');
const paddedPlaintext = Buffer.alloc(slots * chunkBytes);
const nextIv = iv => Buffer.from(crypto.hkdfSync('sha256', iv, salt, info, 16));
let iv = file.subarray(0, 16);
for (let slot = 0; slot < slots; ++slot) {
  const offset = headerBytes + slot * slotBytes;
  const decipher = crypto.createDecipheriv('aes-256-cbc', key, iv);
  decipher.setAutoPadding(false);
  const plaintext = Buffer.concat([decipher.update(file.subarray(offset, offset + chunkBytes)), decipher.final()]);
  assert.equal(plaintext.length, chunkBytes);
  plaintext.copy(paddedPlaintext, slot * chunkBytes);
  iv = nextIv(nextIv(iv));
}
const crc = Uint32Array.from({length: 256}, (_, value) => {
  for (let bit = 0; bit < 8; ++bit) value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1;
  return value >>> 0;
});
function keystream(seed) {
  let state = 0xffffffff;
  for (let i = 0; i < 4; ++i) {
    let byte = (seed >>> (8 * i)) & 255;
    if (byte >= 65 && byte <= 90) byte += 32;
    state = (state >>> 8) ^ crc[(state ^ byte) & 255];
  }
  return (~state) >>> 0;
}
const scrambleSeed = 0xe910d5b8;
let step = scrambleSeed;
const sqlitePadded = Buffer.from(paddedPlaintext);
for (let offset = 0; offset < sqlitePadded.length; offset += 4) {
  sqlitePadded.writeUInt32LE((sqlitePadded.readUInt32LE(offset) ^ keystream(step)) >>> 0, offset);
  step = (step + scrambleSeed + 1) >>> 0;
}
assert(sqlitePadded.subarray(0, 16).equals(Buffer.from('SQLite format 3\0', 'ascii')), 'No SQLite header.');
let pageSize = sqlitePadded.readUInt16BE(16);
if (pageSize === 1) pageSize = 65536;
const headerPages = sqlitePadded.readUInt32BE(28);
const plaintextBytes = paddedPlaintext.length - paddingBytes;
assert.equal(headerPages * pageSize, plaintextBytes, 'SQLite header size disagrees with container padding.');
const database = sqlitePadded.subarray(0, plaintextBytes);

// Verify complete container reconstruction in memory, including untouched MAC
// slots. This does not claim verification of those authentication tags.
const roundTrip = Buffer.from(file);
iv = file.subarray(0, 16);
for (let slot = 0; slot < slots; ++slot) {
  const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
  cipher.setAutoPadding(false);
  const chunk = paddedPlaintext.subarray(slot * chunkBytes, (slot + 1) * chunkBytes);
  const ciphertext = Buffer.concat([cipher.update(chunk), cipher.final()]);
  ciphertext.copy(roundTrip, headerBytes + slot * slotBytes);
  iv = nextIv(nextIv(iv));
}
assert(roundTrip.equals(file), 'Container reconstruction differs from input.');
fs.writeFileSync(outputPath, database, {flag: 'wx'});
const connection = new DatabaseSync(outputPath, {readOnly: true});
let integrity, tables, foreignKeyViolations, carCount;
try {
  integrity = connection.prepare('PRAGMA integrity_check').all();
  assert.equal(integrity.length, 1);
  assert.equal(integrity[0].integrity_check, 'ok', 'SQLite integrity_check failed.');
  tables = connection.prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").all().map(r => r.name);
  foreignKeyViolations = connection.prepare('PRAGMA foreign_key_check').all().length;
  if (tables.includes('Data_Car')) carCount = connection.prepare('SELECT COUNT(*) AS count FROM Data_Car').get().count;
} finally { connection.close(); }
assert.equal(sha256(fs.readFileSync(inputPath)), sha256(file), 'Input changed while running.');
const receipt = {
  completedUtc: new Date().toISOString(), method: 'static-only offline AES-256-CBC / HKDF-SHA256 / CRC32 unscramble',
  input: {path: path.resolve(inputPath), bytes: file.length, sha256: sha256(file)},
  output: {path: path.resolve(outputPath), bytes: database.length, sha256: sha256(database), pageSize, pages: headerPages},
  keySource: constants.provenance, keyCandidate: candidate.name, keySha256: sha256(key),
  slots, paddingBytes, integrity, tableCount: tables.length, carCount, foreignKeyViolations,
  containerRoundTripEqual: true, authenticationTagsVerified: false, inputUnchanged: true,
  downloadedAssemblyLoadedOrExecuted: false, gameLaunched: false, processAttachment: false, dllInjection: false, gameFilesModified: false,
  elapsedMs: Date.now() - started,
};
fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\n', {flag: 'wx'});
console.log(JSON.stringify(receipt, null, 2));
