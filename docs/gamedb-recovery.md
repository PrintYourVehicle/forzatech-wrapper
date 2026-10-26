# FH6 GameDB recovery and conversion workflows

Offline FH6 GameDB decryption succeeded on **2026-10-26**. The wrapper can consume the recovered SQLite cache through its existing database-backed path. Treat earlier assumptions that FH6 GameDB cannot be decrypted, or that generic tire/wheel parameters are the only available inputs, as superseded for the verified database below.

The wrapper now packages a key-free [offline decryptor](../tools/decrypt_fh6_gamedb.cjs) for cache misses. It runs only when `FH6_GAMEDB_KEYS_PATH` points to a locally provisioned, ignored constants JSON file and Node.js supports `node:sqlite`. A fresh checkout does not include the constants or a decrypted database; setting only `CRYPTOTOOL_PATH` still does not recover the verified FH6 input.

## Static-only boundary

The user's boundary for this work is static analysis only. Work on file copies. Do not launch or attach to the game, use a debugger, read game process memory, inject DLLs, install hooks, or modify installed game files or their permissions.

The successful route inspected a publicly distributed crypto runtime as PE/CLR data and decoded its literal constants. That downloaded assembly was never loaded or executed. No game files were uploaded to a third-party service. The installed executable could not be read even by an elevated helper or a backup-mode copy; reading it was unnecessary for the successful recovery.

## Verified checkpoint

These facts apply to the recorded input, not every FH6 build.

| Item | Verified value |
| --- | --- |
| Input | `<FORZA_PATH>/media/stripped/gamedbRC.slt` |
| Encrypted size | 16,123,860 bytes |
| Encrypted SHA-256 | `807dde5a6bf5f2dd7780311e03c8ffafc92160956048e140b9ac87bf0e4d3084` |
| Decrypted size | 16,008,192 bytes |
| Decrypted SHA-256 | `64fcb129c17ab4f24bcf698f5feed0ffe47435ca38aa017c7f1e1afbfee76e97` |
| SQLite layout | 15,633 pages of 1,024 bytes |
| SQLite `integrity_check` | `ok` |
| Tables / `Data_Car` rows | 207 / 682 |
| Foreign-key check | 4,131 reported discrepancies; no repairs made |
| Container reconstruction | Byte-identical to the encrypted input |
| Authentication tags | Not verified; reconstruction preserved the original tag bytes |
| Installed source | SHA-256 unchanged after recovery |

Page integrity and byte-identical reconstruction are distinct from authentication-tag verification, referential completeness, and validation of converted geometry. Do not silently delete or rewrite rows to eliminate the foreign-key findings.

Local checkpoint files are ignored and will not appear in a fresh clone:

- [Recovery status](../temp/gamedb-recovery-20261026/recovery-status.json).
- [Decryption receipt](../temp/gamedb-recovery-20261026/static-decryption-receipt.json).
- [Workflow query evidence](../temp/gamedb-recovery-20261026/workflow-query-evidence.json).
- [Recovered SQLite database](../temp/gamedb-recovery-20261026/gamedbRC.decrypted.sqlite).
- [Working cache copy](../workspace/_gamedb/gamedbRC.584dab6169df.slt).
- [Original offline decryption script](../temp/gamedb-recovery-20261026/decrypt-fh6-static.cjs) and [local decoded constants](../temp/gamedb-recovery-20261026/static-key-candidates.json). These remain ignored local checkpoints. A key-free copy of the script is packaged under `tools/`; the constants are not.

Retain these local files when cleaning scratch data until a durable recovery implementation replaces them. Do not commit the decoded key values, downloaded runtime, or database contents.

## Successful method and provenance

The algorithm reference is HTTPJXRDN's Forza Mod Tool: [GameDb.cs](https://github.com/HTTPJXRDN/Forza-Mod-Tool/blob/main/GameDb.cs), [Scramble.cs](https://github.com/HTTPJXRDN/Forza-Mod-Tool/blob/main/Scramble.cs), and its [published runtime](https://github.com/HTTPJXRDN/Forza-Mod-Tool/tree/main/lib). The links identify upstream sources; the hashes below identify the exact runtime inspected. A later file at the same URL requires fresh inspection.

| Runtime evidence | Value |
| --- | --- |
| File | `FH6LocalCrypto.Runtime.dll` |
| Size | 1,922,560 bytes |
| Git blob SHA-1 | `2ab68a305975d275a8430f2860e45c9f5b4ee09a` |
| SHA-256 | `228c725011651d425ea7b27018361cd11d3b1c2903faebb6ab7f9d4e20ba6ddd` |
| Selected candidate | `k1_GameDB`, usage `GameDB` |
| Data-key SHA-256 | `b3fbfd6e49a7f9d4e144d7094827802c67c19414aeb890fd8604cebf873012c0` |

The legacy TFIT decryptor rejected this input. Recovery then used statically decoded constants and ordinary local cryptographic primitives. It did not require GPU brute force or executing the published runtime.

For the exact runtime fingerprint above, PE/CLR metadata exposed the `Fh6Keys` initializer and a 1,402-byte literal pool at RVA `1920240`. The pool decoder is `decoded[i] = encoded[i] XOR (i & 255) XOR 0xAA`. The constant getter methods provide pool offsets and lengths. In this artifact, method rows 35 and 36 provide the transport values, and row 39 provides the GameDB data-key string. Decode the literals as data and follow the initializer's usage labels. These offsets and method rows are specific to this artifact; do not apply them to a different hash.

The file pipeline is:

1. Read the first 16 bytes as the initial IV, the little-endian padding count at offset 16, and retain the following 16 header bytes. The payload starts at offset 36.
2. Validate that the payload comprises whole 131,088-byte slots: 131,072 ciphertext bytes followed by 16 tag bytes. This input has 123 slots and 113,664 padding bytes.
3. Decrypt each data chunk with AES-256-CBC and no automatic padding. Advance the IV twice between data chunks using HKDF-SHA256: current IV as input key material, transport value 1 as salt, transport value 2 as info, and a 16-byte result. One advance corresponds to the data chunk and one to the tag block.
4. Unscramble the concatenated plaintext using the source's CRC32 XOR stream. For logical word index `q`, the seed is `q + 0xE910D5B8 * (q + 1)` modulo `2^32`. Fold ASCII uppercase bytes to lowercase before the four-byte CRC32 step, using polynomial `0xEDB88320`.
5. Remove the declared padding. Require the SQLite header, page size, and header page count to agree with the resulting length.
6. Open the result read-only, run SQLite integrity checks and consumer queries, record foreign-key findings separately, and retain input/output fingerprints. Container reconstruction can confirm byte preservation, but preserving original tag bytes does not authenticate them.

The [packaged script](../tools/decrypt_fh6_gamedb.cjs) implements this pipeline and refuses to overwrite an existing output or receipt. Its arguments are an encrypted copy, the local constants JSON, a new SQLite output path, and a new receipt path. On a wrapper cache miss, [main.py](../main.py) makes a source copy under the ignored cache directory, runs this script, checks its receipt and SQLite consumer schema, then publishes the validated database and a source-bound receipt. It never overwrites an existing cache or receipt. Reuse the existing validated result unless changed inputs or a missing checkpoint require a new run.

## Cache resolution and reuse

The resolver in [main.py](../main.py) checks readable source databases, then the corresponding decrypted cache, then offline FH6 recovery when explicitly configured, then the legacy crypto helper. It opens databases read-only and checks the columns used by the importer plus SQLite `quick_check`. It can use the validated cache without either decryptor configured.

Cache names are derived by `_decrypted_game_db_cache_path(source_path)`:

- Take the first 12 hexadecimal characters of SHA-1 over the UTF-8 text `resolved_source_path|source_size|source_mtime_ns`.
- Store the decrypted database as `<source_stem>.<signature>.slt` under [workspace/_gamedb/](../workspace/_gamedb/).
- New automatic recovery writes `<cache_name>.receipt.json` beside the cache, with source path, size, nanosecond modification time, source SHA-256, output SHA-256, and method provenance. The receipt does not contain key values. A receipt mismatch makes the cache unavailable for inspection; the wrapper does not erase it.
- The verified install produced `gamedbRC.584dab6169df.slt`. That filename is specific to this source path and metadata. A move, timestamp change, or game update can select a different cache path. Source/output SHA-256 receipts remain the stronger evidence of content identity.
- The original verified cache predates sidecar receipts. The resolver accepts that unreceipted cache only when its source and output hashes match the verified pair above. Other manually provisioned caches need a source-bound receipt or a fresh automatic recovery.

Use the helper to calculate a destination when provisioning another install. Do not put a decrypted database over the installed encrypted source or assume that copying the current cache filename is portable. Confirm that the recovered database matches the new source before provisioning it.

The resolver detects a newly provisioned cache and source metadata changes while running. A fresh-process status check is still useful for setup diagnostics:

```powershell
uv run python -c "import main; print(main.game_db_status())"
```

Expect `decrypted cache: ...` for a matching recovered cache. Existing conversion logs also report `[GameDB]` status. An unavailable status from a fresh install means the cache/setup needs attention; the legacy-helper error message does not establish that the verified offline method cannot work.

## Workflows affected

The observations below were checked with read-only SQL and source inspection on 2026-10-26. All 682 GameDB cars have a complete stock tire/body join, and all 667 installed car archives match a GameDB `MediaName` (26 differ only by letter case). Its 34 distinct stock tire compounds each have a matching `tire_<compound>.zip` in this install's shared tire directory. Archive presence does not by itself validate mesh import.

| Workflow | Available database-backed behavior | Validation boundary |
| --- | --- | --- |
| Shared tire selection | The wrapper and importer use a parameterized, case-insensitive exact `MediaName` match and the stock compound/body rows. | All 682 database cars matched; matching shared archives were found for all 34 stock compounds. |
| Tire and rim sizing | The importer reads front/rear width, aspect ratio, and wheel diameter from `Data_Car`. | Two full native conversions confirmed stock values and plausible mesh bounds; other cars and upgrades remain untested. |
| Wheel and suspension placement | The importer reads wheelbase, front/rear track, ride heights, and wheelbase-center offsets from `Data_CarBody`. | Complete per-car stock data selects `suspension_transform_type = 2`; two full native conversions placed all eight tire centers within 0.27 mm of the derived positions. |
| Catalog years | Matching `Data_Car.Year` values replace two-digit filename guesses while make/model names retain compatibility labels. | 170 of 667 filename-derived years differed from the database; database make/model strings are localization keys, so they are not display labels. |
| GUI, TUI, CLI, OBJ and bundle conversions | All interfaces use the shared conversion job and script generator. A bundle succeeds only when all three variants succeed. | Native CLI full, body, interior, bundle zip, and OBJ exports passed for the sampled cars; GUI/TUI interaction remains untested. |
| Compatibility fallback | A car with missing or incomplete stock import data uses the same fallback for script settings and shared tire extraction. | Keep known overrides for unsupported or unavailable databases. Fixture tests cover the fallback path. |

Concrete stock-query results, using the stock car-body row as the importer does by default:

| Media name | Stock compound | Front tire / rim | Rear tire / rim | Model wheelbase |
| --- | --- | --- | --- | --- |
| `BMW_M3E92_08` | `Street` | 245/40 R18 | 265/40 R18 | 2.761607 m |
| `FER_LaFerrari_13` | `Semi_Slick` | 265/30 R19 | 345/30 R20 | 2.65 m |

For LaFerrari, the compatibility sizing override agrees with these tire dimensions, but its generic `WET_c` compound does not represent the stock query result. For BMW M3 E92, the generic 295/35 R19 rear fallback also differs from the queried stock data. The database-backed path selected `Semi_Slick` and `Street`, respectively.

## Native conversion checkpoint

On 2026-10-26, Blender 5.1.1 saved full `.blend` conversions for `BMW_M3E92_08` (568 meshes) and `FER_LaFerrari_13` (513 meshes) through the validated cache. The [ignored conversion receipt](../workspace/review-20261026/conversion-receipt.json) records source/cache/output hashes and generated settings. The [ignored geometry receipt](../workspace/review-20261026/geometry-validation.json) records stock values and measured world-space bounds. [BMW](../workspace/review-20261026/BMW_M3E92_08.full-car.png) and [LaFerrari](../workspace/review-20261026/FER_LaFerrari_13.full-car.png) diagnostic renders show plausible wheel-arch fit and ride height.

All eight tire centers were within 0.27 mm of positions derived from the stock GameDB. Wheel centers were within 0.29 mm of their tires. The BMW brake/control-arm left/right bounds differ by about 27 mm; source carbin matrices are also asymmetric, so no corrective transform was applied. This checkpoint checks two stock cars and visible geometry, not all cars, upgrades, materials, or artistic parity.

The [ignored mode receipt](../workspace/review-20261026/mode-validation.json) records a BMW bundle zip with three CRC-checked `.blend` entries. Full/body/interior scenes contain 568/213/149 meshes; the body scene contains no tire meshes. A LaFerrari OBJ/MTL export also completed, with 513 OBJ objects. These checks exercise the shared CLI path after the importer stream fix.

## Follow-up work

1. Validate more cars and upgrade variants, and exercise GUI/TUI interaction. Keep native evidence separate from earlier outputs.
2. Revisit manual tire specifications and any workaround introduced because GameDB was unavailable. Retain compatibility behavior for other inputs until their replacements have conversion evidence.
3. Use the database for broader car/upgrade metadata only after inspecting the required schema and joins. The existing join coverage does not remove the 4,131 reported foreign-key findings or prove coverage for new consumers.

Recovery, SQL query coverage, archive availability, and two full Blender conversions have been verified. Authentication tags were not verified, and the native sample remains limited to the two cars above.
