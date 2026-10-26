# ForzaTech Wrapper

A PySide6 desktop UI plus automation CLI around [ForzaTech-extraction-tools](https://github.com/Doliman100/ForzaTech-extraction-tools) for batch-converting FH6 car models to Blender `.blend` files.

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)

## Features

- **Interactive car selection** — browse and search from all available car zips in a desktop UI
- **Automation CLI** — run repeatable car conversion jobs from scripts and tests
- **Sandkasse test output** — send conversion outputs into a neighboring Sandkasse data asset folder
- **Search/filter** — quickly find cars by name with real-time filtering
- **Full export** — imports the complete car model (body, engine, interior, wheels, tires, brakes)
- **Body-only export** — imports only exterior body panels (bumpers, fenders, doors, hood, wing, lights) — ideal for 3D printing
- **Interior-only export** — imports only interior components (dash, seats, console)
- **OBJ export** — exports car models as `.obj` files for use in other 3D software
- **Extract Bundle** — exports all modes (full, body, interior) into a folder per car
- **Extract Bundle Zip** — same as above but packaged as a `.zip` per car
- **Batch processing** — convert multiple cars in one go
- **Parallel conversion** — runs multiple Blender instances concurrently for maximum throughput
- **Automatic cleanup** — extracted media files are removed after conversion to save disk space
- **Automatic zip extraction** — handles the game's zip-packed car format
- **Shared tire extraction** — expands the needed `_library/scene/tires/tire_*.zip` mesh before Blender imports
- **GameDB cache** — uses a readable source or source-matched decrypted cache for stock tire compounds, tire/rim sizes, and placement data; optional [offline FH6 recovery](docs/gamedb-recovery.md) can fill a cache miss from locally provisioned constants
- **Source-backed catalog years** — uses `Data_Car.Year` for matching cars, with filename years as a fallback
- **No GameDB required** — falls back to defaults/overrides when no decrypted database is available

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [Blender 4.1+](https://www.blender.org/download/) (tested with 5.1.1)
- Forza Horizon 6 game files (specifically `media\Cars\*.zip`)

## Desktop Interface
<img width="1918" height="1009" alt="TUI image" src="https://github.com/user-attachments/assets/4a858c4a-ddea-4170-a818-41f236958c9a" />



## Outputs

|   |   |
|---|---|
| <img width="400" height="300" alt="image" src="https://github.com/user-attachments/assets/b52982b3-f51c-43ed-b50e-e69e354c9432" /> | <img width="400" height="300" alt="image" src="https://github.com/user-attachments/assets/99ad7a9c-aff0-46b1-a382-829c70be8a01" /> |
| <img width="400" height="300" alt="image" src="https://github.com/user-attachments/assets/e14f1161-d8b6-4e47-a514-d89f68fe22b0" /> | <img width="400" height="300" alt="image" src="https://github.com/user-attachments/assets/e652d161-7171-418d-9591-5743c0c149a9" /> |
| <img width="400" height="300" alt="image" src="https://github.com/user-attachments/assets/c361a976-2da9-446d-844f-1a93e0fd29c2" /> | <img width="400" height="300" alt="image" src="https://github.com/user-attachments/assets/6b25369f-eb32-4a0f-bb4e-6505a58b0af7" />  |

> If you're wondering why these have only body panels, it's because we only need them for 3D printing — not the windows, engine, etc. This repo is specifically for PrintYourVehicle and might not be useful for other cases. It also contains heavily AI-generated code, so be cautious about touching it lol.

## Setup

1. Clone with submodules:
   ```bash
   git clone --recurse-submodules https://github.com/PrintYourVehicle/forzatech-wrapper.git
   cd forzatech-wrapper
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

3. Copy `.env.example` to `.env` and edit paths:
   ```bash
   cp .env.example .env
   ```

   ```env
   FORZA_PATH=D:\games\Forza Horizon 6
   BLENDER_PATH=D:\path\to\blender.exe
   CRYPTOTOOL_PATH=D:\path\to\CryptoTool.exe
   # Optional: FH6_GAMEDB_KEYS_PATH=D:\path\to\static-key-candidates.json
   MAX_WORKERS=4
   ```

   Both decryption settings are optional. `FH6_GAMEDB_KEYS_PATH` points to a
   local, ignored JSON file of statically decoded FH6 constants and requires a
   Node runtime with `node:sqlite` (validated with Node 22.19.0). The wrapper
   uses the packaged key-free offline decryptor on a cache miss, then tries
   `CRYPTOTOOL_PATH` as a legacy helper. A matching validated cache works
   without either setting. No key values or decrypted databases are included
   in this repository. See the [recovery guide](docs/gamedb-recovery.md) for
   provenance, validation, and the static-only workflow.

## Usage

```bash
uv run python main.py
```

This launches the PySide6 desktop UI by default. The previous terminal UI is still available:

```bash
uv run python main.py tui
```

### Automation CLI

List available car assets:

```bash
uv run python main.py list --filter supra
```

Convert one car by exact stem or unique substring:

```bash
uv run python main.py convert --car Supra --mode full --format blend
uv run python main.py convert --car Supra --mode body --format obj
uv run python main.py convert --car Supra --format bundle-zip
```

Run a full test extraction/conversion bundle for one car and write it into a neighboring Sandkasse checkout when one is found:

```bash
uv run python main.py test-car Supra
```

Use `--output-dir` for a custom destination, `--workspace` for extraction scratch space, and `--workers` to control parallel Blender runs.

### Controls

| Key | Action |
|-----|--------|
| `a` | Select all cars |
| `n` | Deselect all |
| `q` | Quit |

### Buttons

- **Convert (Full)** — full model with all parts
- **Convert (Body Only)** — exterior panels only (no engine, interior, wheels, windows)
- **Convert (Interior Only)** — interior components only (dash, seats, console)
- **Export OBJ** — full model exported as `.obj` + `.mtl`
- **Extract Bundle** — exports all modes (full, body, interior) into a folder per car
- **Extract Bundle Zip** — same as above but packaged as a `.zip` per car
- **Save Log** — saves conversion log to `workspace/log.txt`

### Output

Converted files are saved to the `workspace/` directory:
- Full: `workspace/<car_name>.blend`
- Body only: `workspace/<car_name>_body.blend`
- Interior only: `workspace/<car_name>_interior.blend`
- OBJ: `workspace/<car_name>.obj` + `workspace/<car_name>.mtl`
- Bundle: `workspace/<car_name>/` (folder with all .blend variants)
- Bundle Zip: `workspace/<car_name>.zip`

## How it works

1. Lists all `.zip` files from `<FORZA_PATH>/media/Cars/`
2. Extracts selected car zips into `workspace/Media/Cars/<name>/`
3. Resolves a readable GameDB source or matching decrypted cache. On a cache miss, it can run the offline FH6 decryptor with explicitly provisioned local constants, then the optional legacy `CryptoTool.exe`
4. Extracts shared `_library` material parents and the tire mesh archive the importer will request
5. Generates a patched version of `carbin_importer.py` with:
   - Correct paths injected
   - GameDB usage when a readable or decrypted DB has a complete stock row for the car, otherwise compatibility tire/wheel parameters
   - Missing file handling for unavailable shared assets
   - Body-only filtering (when selected)
6. Runs Blender in background mode with the patched script
7. Saves the resulting `.blend` file

## Project Structure

```
forzatech-wrapper/
├── main.py                      # GUI, TUI, and automation CLI
├── .env.example                 # Environment variable template
├── pyproject.toml               # Project config & dependencies
├── uv.lock                      # Locked dependencies
├── AGENTS.md                    # Project guidance and static-only GameDB boundary
├── docs/gamedb-recovery.md       # Verified offline recovery and workflow implications
├── tools/decrypt_fh6_gamedb.cjs  # Key-free offline FH6 cache-miss decryptor
├── tests/test_gamedb_import.py   # Database, fallback, and export-status checks
├── ForzaTech-extraction-tools/  # Submodule - carbin importer scripts
└── workspace/                   # Output directory (gitignored)
```

## Limitations

- **FH6 cache provisioning** — automatic offline recovery requires locally decoded constants and a Node runtime with `node:sqlite`; a fresh clone has neither the constants nor a decrypted cache. See the [GameDB recovery guide](docs/gamedb-recovery.md). The older helper alone did not decrypt the verified input.
- **Encrypted GameDB fallback** — when GameDB decryption is unavailable or fails, tire compound/specs use per-car overrides where known, otherwise the wrapper default (`WET_c`) and generic sizing data
- **Shared tire availability** — tire meshes require matching zip files under `<FORZA_PATH>/media/cars/_library/scene/tires/`
- **FH6 only tested** — should work with FH5 and earlier but paths may need adjustment

## Credits

- [ForzaTech-extraction-tools](https://github.com/Doliman100/ForzaTech-extraction-tools) — carbin/modelbin importer scripts
