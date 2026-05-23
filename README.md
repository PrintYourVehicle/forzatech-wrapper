# ForzaTech Wrapper

A TUI (Terminal User Interface) wrapper around [ForzaTech-extraction-tools](https://github.com/user/ForzaTech-extraction-tools) for batch-converting Forza Horizon 6 car models to Blender `.blend` files.

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)

## Features

- **Interactive car selection** — browse and select from all available car zips
- **Full export** — imports the complete car model (body, engine, interior, wheels, brakes)
- **Body-only export** — imports only exterior body panels (bumpers, fenders, doors, hood, wing, lights) — ideal for 3D printing
- **Batch processing** — convert multiple cars in one go
- **Automatic zip extraction** — handles the game's zip-packed car format
- **No GameDB required** — works without the decrypted database file (`use_db = False`)

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [Blender 4.1+](https://www.blender.org/download/) (tested with 5.1.1)
- Forza Horizon 6 game files (specifically `media\Cars\*.zip`)

## Setup

1. Clone with submodules:
   ```bash
   git clone --recurse-submodules https://github.com/user/forzatech-wrapper.git
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
- **Save Log** — saves conversion log to `workspace/log.txt`

### Output

Converted `.blend` files are saved to the `workspace/` directory:
- Full: `workspace/<car_name>.blend`
- Body only: `workspace/<car_name>_body.blend`

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
