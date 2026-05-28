# ForzaTech Wrapper

A TUI (Terminal User Interface) wrapper around [ForzaTech-extraction-tools](https://github.com/Doliman100/ForzaTech-extraction-tools) for batch-converting FH6 car models to Blender `.blend` files.

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)

## Features

- **Interactive car selection** — browse and search from all available car zips
- **Search/filter** — quickly find cars by name with real-time filtering
- **Full export** — imports the complete car model (body, engine, interior, wheels, brakes)
- **Body-only export** — imports only exterior body panels (bumpers, fenders, doors, hood, wing, lights) — ideal for 3D printing
- **Interior-only export** — imports only interior components (dash, seats, console)
- **OBJ export** — exports car models as `.obj` files for use in other 3D software
- **Extract Bundle** — exports all modes (full, body, interior) into a folder per car
- **Extract Bundle Zip** — same as above but packaged as a `.zip` per car
- **Batch processing** — convert multiple cars in one go
- **Parallel conversion** — runs multiple Blender instances concurrently for maximum throughput
- **Automatic cleanup** — extracted media files are removed after conversion to save disk space
- **Automatic zip extraction** — handles the game's zip-packed car format
- **No GameDB required** — works without the decrypted database file (`use_db = False`)

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [Blender 4.1+](https://www.blender.org/download/) (tested with 5.1.1)
- Forza Horizon 6 game files (specifically `media\Cars\*.zip`)

## TUI Interface
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
   MAX_WORKERS=4
   ```

## Usage

```bash
uv run python main.py
```

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
3. Generates a patched version of `carbin_importer.py` with:
   - Correct paths injected
   - `use_db = False` (no decrypted GameDB needed)
   - Default tire/wheel parameters
   - Missing file handling (skips unavailable tires)
   - Body-only filtering (when selected)
4. Runs Blender in background mode with the patched script
5. Saves the resulting `.blend` file

## Project Structure

```
forzatech-wrapper/
├── main.py                      # TUI application
├── .env.example                 # Environment variable template
├── pyproject.toml               # Project config & dependencies
├── uv.lock                      # Locked dependencies
├── ForzaTech-extraction-tools/  # Submodule - carbin importer scripts
└── workspace/                   # Output directory (gitignored)
```

## Limitations

- **No GameDB** — wheel positioning and tire scaling use generic defaults since FH6's GameDB decryption is not yet supported by the crypto tool
- **Missing tires** — tire models are skipped (they reference shared `_library` files not included in individual car zips)
- **FH6 only tested** — should work with FH5 and earlier but paths may need adjustment

## Credits

- [ForzaTech-extraction-tools](https://github.com/Doliman100/ForzaTech-extraction-tools) — carbin/modelbin importer scripts
