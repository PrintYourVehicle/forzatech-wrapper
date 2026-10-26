"""ForzaTech Wrapper - GUI/CLI for converting Forza car models to Blender."""

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import json
import math
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from functools import lru_cache

from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Log, SelectionList
from textual.widgets.selection_list import Selection

load_dotenv()

FORZA_PATH = Path(os.getenv("FORZA_PATH", ""))
BLENDER_PATH = Path(os.getenv("BLENDER_PATH", ""))
CRYPTOTOOL_PATH = Path(os.getenv("CRYPTOTOOL_PATH", "")) if os.getenv("CRYPTOTOOL_PATH") else None
CARS_PATH = FORZA_PATH / "media" / "Cars"
# Shared tiling/material textures (carbon fiber, alcantara, leather, etc.).
# Lives in media/cars/_library/Textures.zip, independent of individual car zips.
SHARED_TEXTURES_ZIP = FORZA_PATH / "media" / "cars" / "_library" / "Textures.zip"
SHARED_MATERIALS_ZIP = FORZA_PATH / "media" / "cars" / "_library" / "Materials.zip"
SHARED_TIRE_ZIP_DIR = FORZA_PATH / "media" / "cars" / "_library" / "scene" / "tires"
SHARED_TEX_DIR = Path(__file__).parent / "workspace" / "_shared_textures"
WORKSPACE = Path(__file__).parent / "workspace"
SCRIPT_PATH = Path(__file__).parent / "ForzaTech-extraction-tools" / "scripts" / "carbin_importer.py"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", max(1, os.cpu_count() or 4)))
DEFAULT_TIRE_MODEL_NAME = "WET_c"
DECRYPTED_GAME_DB_DIR = WORKSPACE / "_gamedb"


@dataclass(frozen=True)
class TireImportSpec:
    tire_model_name: str
    front_width_mm: int
    front_aspect: int
    front_wheel_diameter_in: int
    rear_width_mm: int
    rear_aspect: int
    rear_wheel_diameter_in: int
    model_wheelbase: float = 2.6
    model_front_track_outer: float = 1.6
    model_rear_track_outer: float = 1.6
    bottom_center_wheelbase_pos_x: float = 0.0
    bottom_center_wheelbase_pos_y: float = 0.0
    bottom_center_wheelbase_pos_z: float = 0.0
    model_front_stock_ride_height: float = 0.1
    model_rear_stock_ride_height: float = 0.1


DEFAULT_TIRE_IMPORT_SPEC = TireImportSpec(
    tire_model_name=DEFAULT_TIRE_MODEL_NAME,
    front_width_mm=245,
    front_aspect=40,
    front_wheel_diameter_in=18,
    rear_width_mm=295,
    rear_aspect=35,
    rear_wheel_diameter_in=19,
)

CAR_TIRE_SPEC_OVERRIDES: dict[str, TireImportSpec] = {
    # Compatibility sizing for installations without a matching readable GameDB.
    "FER_LAFERRARI_13": TireImportSpec(
        tire_model_name=DEFAULT_TIRE_MODEL_NAME,
        front_width_mm=265,
        front_aspect=30,
        front_wheel_diameter_in=19,
        rear_width_mm=345,
        rear_aspect=30,
        rear_wheel_diameter_in=20,
        model_wheelbase=2.65,
    ),
}

# Part types we want for body-only export (exterior panels for 3D printing)
BODY_PART_TYPES = {2, 9, 34, 35, 36, 37}  # CarBody, RearWing, FrontBumper, RearBumper, Hood, SideSkirts

# Model type names to EXCLUDE in body-only mode (interior/mechanical)
EXCLUDED_MODEL_TYPES = {
    "Chassis", "Engine", "Dash", "Floor", "Seats", "CenterConsole",
    "CenterStack", "Pillar", "InteriorWindows", "InteriorLOD",
    "Tires", "ControlArm", "Gauges", "Details", "Windows",
    "PrimaryLights", "SecondaryLights",
}

# Model type names to KEEP in interior-only mode
INTERIOR_MODEL_TYPES = {
    "Dash", "Floor", "Seats", "CenterConsole", "CenterStack",
    "Pillar", "InteriorWindows", "InteriorLOD", "Gauges", "Details",
}


_GAME_DB_CACHE_MISS = object()
_game_db_path_cache: Path | object | None = _GAME_DB_CACHE_MISS
_game_db_path_cache_key: tuple | None = None
_game_db_last_error = ""
_game_db_resolve_lock = threading.Lock()
_FH6_KEYS_ENV = "FH6_GAMEDB_KEYS_PATH"
_FH6_DECRYPT_SCRIPT = Path(__file__).parent / "tools" / "decrypt_fh6_gamedb.cjs"
_VERIFIED_FH6_SOURCE_SHA256 = "807dde5a6bf5f2dd7780311e03c8ffafc92160956048e140b9ac87bf0e4d3084"
_VERIFIED_FH6_OUTPUT_SHA256 = "64fcb129c17ab4f24bcf698f5feed0ffe47435ca38aa017c7f1e1afbfee76e97"


def _game_db_source_candidates() -> tuple[Path, ...]:
    return (
        FORZA_PATH / "media" / "stripped" / "gamedbRC.slt",
        FORZA_PATH / "media" / "db" / "gamedb.slt",
    )


def _is_readable_sqlite_db(candidate: Path) -> bool:
    if not candidate.is_file():
        return False
    try:
        # Open read-only: sqlite3.connect(path) otherwise creates missing files
        # and may create a journal next to an installed or cached database.
        with closing(sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            # Check the columns consumed by both the wrapper and the importer.
            # A valid but unrelated SQLite file is not a usable GameDB.
            connection.execute("""
                SELECT Data_Car.MediaName, Data_Car.Id,
                    Data_Car.FrontTireWidthMM, Data_Car.FrontTireAspect,
                    Data_Car.FrontWheelDiameterIN, Data_Car.RearTireWidthMM,
                    Data_Car.RearTireAspect, Data_Car.RearWheelDiameterIN,
                    List_UpgradeTireCompound.TireModelName,
                    List_UpgradeTireCompound.IsStock,
                    List_UpgradeCarBody.CarBodyID,
                    Data_CarBody.ModelWheelbase,
                    Data_CarBody.ModelFrontTrackOuter,
                    Data_CarBody.ModelRearTrackOuter,
                    Data_CarBody.ModelFrontStockRideHeight,
                    Data_CarBody.ModelRearStockRideHeight,
                    Data_CarBody.BottomCenterWheelbasePosx,
                    Data_CarBody.BottomCenterWheelbasePosy,
                    Data_CarBody.BottomCenterWheelbasePosZ
                FROM Data_Car
                    JOIN List_UpgradeTireCompound
                        ON List_UpgradeTireCompound.Ordinal = Data_Car.Id
                    JOIN List_UpgradeCarBody
                        ON List_UpgradeCarBody.Ordinal = Data_Car.Id
                    JOIN Data_CarBody
                        ON Data_CarBody.Id = List_UpgradeCarBody.CarBodyID
                LIMIT 0
            """)
            return connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    except (OSError, ValueError, sqlite3.DatabaseError):
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _crypto_tool_path() -> Path | None:
    candidates: list[Path] = []
    if CRYPTOTOOL_PATH is not None:
        candidates.append(CRYPTOTOOL_PATH)
    candidates.extend(
        (
            Path(__file__).parent / "CryptoTool.exe",
            Path(__file__).parent / "ForzaTech-crypto-tool" / "CryptoTool.exe",
            Path(__file__).parent / "ForzaTech-crypto-tool" / "CryptoTool" / "CryptoTool.exe",
            Path(__file__).parent / "ForzaTech-crypto-tool" / "x64" / "Release" / "CryptoTool.exe",
            Path(__file__).parent / "ForzaTech-crypto-tool" / "CryptoTool" / "x64" / "Release" / "CryptoTool.exe",
            Path(__file__).parent / "temp" / "ForzaTech-crypto-tool" / "CryptoTool.exe",
            Path(__file__).parent / "temp" / "ForzaTech-crypto-tool" / "CryptoTool" / "CryptoTool.exe",
            Path(__file__).parent / "temp" / "ForzaTech-crypto-tool" / "x64" / "Release" / "CryptoTool.exe",
            Path(__file__).parent / "temp" / "ForzaTech-crypto-tool" / "CryptoTool" / "x64" / "Release" / "CryptoTool.exe",
        )
    )
    which_result = shutil.which("CryptoTool.exe")
    if which_result:
        candidates.append(Path(which_result))

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _decrypted_game_db_cache_path(source_path: Path) -> Path:
    stat = source_path.stat()
    signature = hashlib.sha1(
        f"{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    return DECRYPTED_GAME_DB_DIR / f"{source_path.stem}.{signature}.slt"


def _game_db_cache_receipt_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(cache_path.suffix + ".receipt.json")


def _game_db_file_state(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns) if path.is_file() else None
    except OSError:
        return None


def _game_db_resolution_state() -> tuple:
    """Notice newly provisioned caches and changed sources without a restart."""
    state: list[tuple] = []
    for source in _game_db_source_candidates():
        source_state = _game_db_file_state(source)
        if source_state is None:
            state.append((str(source), None))
            continue
        try:
            cache = _decrypted_game_db_cache_path(source)
        except OSError:
            state.append((str(source), source_state, None))
            continue
        state.append((str(source), source_state, str(cache), _game_db_file_state(cache),
                      _game_db_file_state(_game_db_cache_receipt_path(cache))))
    constants = os.getenv(_FH6_KEYS_ENV, "")
    state.append((constants, _game_db_file_state(Path(constants)) if constants else None))
    return tuple(state)


def _cache_receipt_matches(source_path: Path, cache_path: Path) -> bool:
    receipt_path = _game_db_cache_receipt_path(cache_path)
    if not receipt_path.exists():
        # Accept the recorded recovery's old cache by its published pair of
        # content hashes. Other manual caches need a source-bound receipt.
        try:
            return (_sha256_file(source_path) == _VERIFIED_FH6_SOURCE_SHA256
                    and _sha256_file(cache_path) == _VERIFIED_FH6_OUTPUT_SHA256)
        except OSError:
            return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        source = receipt["source"]
        output = receipt["output"]
        stat = source_path.stat()
        return (
            source["path"] == str(source_path.resolve())
            and source["bytes"] == stat.st_size
            and source["mtimeNs"] == stat.st_mtime_ns
            and source["sha256"] == _sha256_file(source_path)
            and output["bytes"] == cache_path.stat().st_size
            and output["sha256"] == _sha256_file(cache_path)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _recover_game_db_to_cache(source_path: Path, method: str,
                              command: Callable[[Path, Path, Path], list[str]]) -> Path | None:
    """Recover from a file copy and publish only a validated result and receipt."""
    global _game_db_last_error
    try:
        output_path = _decrypted_game_db_cache_path(source_path)
    except OSError:
        _game_db_last_error = "GameDB source disappeared before recovery"
        return None
    receipt_path = _game_db_cache_receipt_path(output_path)
    if output_path.exists() or receipt_path.exists():
        _game_db_last_error = f"cache or receipt already exists and needs inspection: {output_path}"
        return None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        source_stat = source_path.stat()
        source_hash = _sha256_file(source_path)
    except OSError as error:
        _game_db_last_error = f"cannot prepare GameDB recovery: {type(error).__name__}"
        return None
    with tempfile.TemporaryDirectory(prefix="gamedb-recovery-", dir=output_path.parent) as scratch:
        scratch_path = Path(scratch)
        source_copy = scratch_path / source_path.name
        candidate = scratch_path / "decrypted.slt"
        method_receipt = scratch_path / "method-receipt.json"
        try:
            shutil.copy2(source_path, source_copy)
            copy_matches = (_sha256_file(source_copy) == source_hash
                            and _sha256_file(source_path) == source_hash)
        except OSError:
            copy_matches = False
        if not copy_matches:
            _game_db_last_error = "GameDB source changed while making a recovery copy"
            return None
        try:
            result = subprocess.run(command(source_copy, candidate, method_receipt),
                                    capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as error:
            _game_db_last_error = f"{method} failed to start or timed out: {type(error).__name__}"
            return None
        if result.returncode != 0 or not _is_readable_sqlite_db(candidate):
            _game_db_last_error = f"{method} did not produce a readable GameDB"
            return None
        if (_game_db_file_state(source_path) != (source_stat.st_size, source_stat.st_mtime_ns)
                or _sha256_file(source_path) != source_hash):
            _game_db_last_error = "GameDB source changed during recovery"
            return None
        output_hash = _sha256_file(candidate)
        method_details = None
        if method_receipt.exists():
            try:
                method_details = json.loads(method_receipt.read_text(encoding="utf-8"))
                if (method_details["input"]["sha256"] != source_hash
                        or method_details["output"]["sha256"] != output_hash
                        or method_details["integrity"] != [{"integrity_check": "ok"}]
                        or not method_details["containerRoundTripEqual"]
                        or not method_details["inputUnchanged"]):
                    _game_db_last_error = f"{method} receipt did not validate"
                    return None
            except (OSError, ValueError, KeyError, TypeError):
                _game_db_last_error = f"{method} receipt was invalid"
                return None
        elif method == "FH6 offline decryptor":
            _game_db_last_error = "FH6 offline decryptor did not write a receipt"
            return None
        receipt = {
            "method": method,
            "source": {"path": str(source_path.resolve()), "bytes": source_stat.st_size,
                       "mtimeNs": source_stat.st_mtime_ns, "sha256": source_hash},
            "output": {"bytes": candidate.stat().st_size, "sha256": output_hash},
            "recovery": method_details,
        }
        staged_receipt = scratch_path / "cache-receipt.json"
        staged_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        # Hard links publish complete files without replacing anything that
        # appeared concurrently in another wrapper process.
        if output_path.exists() or receipt_path.exists():
            _game_db_last_error = "GameDB cache appeared during recovery; existing files were preserved"
            return None
        published_output = False
        try:
            os.link(candidate, output_path)
            published_output = True
            os.link(staged_receipt, receipt_path)
        except OSError:
            if published_output:
                output_path.unlink()
            _game_db_last_error = "GameDB cache publication failed; inspect the cache directory"
            return None
        _game_db_last_error = ""
        return output_path


def _decrypt_game_db_with_fh6_static(source_path: Path) -> Path | None:
    global _game_db_last_error
    constants_path = os.getenv(_FH6_KEYS_ENV, "")
    if not constants_path:
        return None
    constants = Path(constants_path).expanduser()
    node = shutil.which("node")
    if not constants.is_file():
        _game_db_last_error = f"{_FH6_KEYS_ENV} does not name a readable local file"
        return None
    if node is None or not _FH6_DECRYPT_SCRIPT.is_file():
        _game_db_last_error = "FH6 offline recovery requires Node.js and the packaged decryptor"
        return None
    return _recover_game_db_to_cache(
        source_path, "FH6 offline decryptor",
        lambda copy, output, receipt: [node, str(_FH6_DECRYPT_SCRIPT), str(copy),
                                       str(constants), str(output), str(receipt)],
    )


def _decrypt_game_db_with_crypto_tool(source_path: Path) -> Path | None:
    tool_path = _crypto_tool_path()
    if tool_path is None:
        return None
    return _recover_game_db_to_cache(
        source_path, "legacy CryptoTool.exe",
        lambda copy, output, receipt: [str(tool_path), "-v", "0", "-i", str(copy),
                                       "-o", str(output), "-y"],
    )


def _readable_game_db_path() -> Path | None:
    """Return a usable SQLite GameDB path, decrypting and caching it if needed."""
    global _game_db_path_cache, _game_db_path_cache_key, _game_db_last_error
    state = _game_db_resolution_state()
    if _game_db_path_cache_key == state and _game_db_path_cache is not _GAME_DB_CACHE_MISS:
        return _game_db_path_cache if isinstance(_game_db_path_cache, Path) else None

    with _game_db_resolve_lock:
        state = _game_db_resolution_state()
        if _game_db_path_cache_key == state and _game_db_path_cache is not _GAME_DB_CACHE_MISS:
            return _game_db_path_cache if isinstance(_game_db_path_cache, Path) else None

        _game_db_last_error = ""
        source_candidates = _game_db_source_candidates()
        for candidate in source_candidates:
            if _is_readable_sqlite_db(candidate):
                _game_db_path_cache = candidate
                _game_db_path_cache_key = _game_db_resolution_state()
                return candidate

        for candidate in source_candidates:
            if not candidate.is_file():
                continue
            try:
                cached_path = _decrypted_game_db_cache_path(candidate)
            except OSError:
                _game_db_last_error = f"GameDB source disappeared: {candidate}"
                continue
            if _is_readable_sqlite_db(cached_path) and _cache_receipt_matches(candidate, cached_path):
                _game_db_path_cache = cached_path
                _game_db_path_cache_key = _game_db_resolution_state()
                return cached_path
            if cached_path.exists() or _game_db_cache_receipt_path(cached_path).exists():
                _game_db_last_error = f"cache or receipt needs inspection: {cached_path}"
                continue
            try:
                with candidate.open("rb") as source_stream:
                    sqlite_header = source_stream.read(16) == b"SQLite format 3\0"
            except OSError:
                sqlite_header = False
            if sqlite_header:
                _game_db_last_error = f"readable SQLite source failed GameDB schema or integrity checks: {candidate}"
                continue
            decrypted_path = _decrypt_game_db_with_fh6_static(candidate)
            fh6_error = _game_db_last_error
            if decrypted_path is None:
                decrypted_path = _decrypt_game_db_with_crypto_tool(candidate)
                if decrypted_path is None and fh6_error and _game_db_last_error != fh6_error:
                    _game_db_last_error = f"{fh6_error}; {_game_db_last_error}"
            if decrypted_path is not None:
                _game_db_path_cache = decrypted_path
                _game_db_path_cache_key = _game_db_resolution_state()
                return decrypted_path

        _game_db_path_cache = None
        _game_db_path_cache_key = _game_db_resolution_state()
        if not _game_db_last_error:
            _game_db_last_error = "no readable source or matching decrypted cache"
        return None


def game_db_status() -> str:
    game_db_path = _readable_game_db_path()
    if game_db_path is not None:
        if DECRYPTED_GAME_DB_DIR in game_db_path.parents:
            return f"decrypted cache: {game_db_path}"
        return f"readable: {game_db_path}"
    if not os.getenv(_FH6_KEYS_ENV) and _crypto_tool_path() is None:
        return (f"unavailable: {_game_db_last_error}; provision a matching cache or set "
                f"{_FH6_KEYS_ENV} to local FH6 constants for offline recovery")
    return f"unavailable: {_game_db_last_error}"


def get_car_zips() -> list[Path]:
    if not CARS_PATH.exists():
        return []
    return sorted(
        [f for f in CARS_PATH.iterdir() if f.suffix == ".zip" and "_Traffic_" not in f.stem],
        key=lambda p: p.stem.upper(),
    )


MANUFACTURER_PREFIXES = {
    "ABT": "Abarth",
    "ACU": "Acura",
    "ALF": "Alfa Romeo",
    "AMC": "AMC",
    "AMG": "Mercedes-AMG",
    "AMR": "Aston Martin",
    "ASC": "Ascari",
    "ASP": "Aston Martin",
    "AUD": "Audi",
    "AUS": "Austin",
    "BAC": "BAC",
    "BEN": "Bentley",
    "BMW": "BMW",
    "BUG": "Bugatti",
    "CAD": "Cadillac",
    "CHE": "Chevrolet",
    "CHR": "Chrysler",
    "CIT": "Citroen",
    "DOD": "Dodge",
    "FER": "Ferrari",
    "FIAT": "Fiat",
    "FOR": "Ford",
    "HON": "Honda",
    "HYU": "Hyundai",
    "JAG": "Jaguar",
    "KOE": "Koenigsegg",
    "LAM": "Lamborghini",
    "LAN": "Lancia",
    "LEX": "Lexus",
    "LOT": "Lotus",
    "MAS": "Maserati",
    "MAZ": "Mazda",
    "MCL": "McLaren",
    "MER": "Mercedes-Benz",
    "MG": "MG",
    "MIN": "MINI",
    "MIT": "Mitsubishi",
    "NIS": "Nissan",
    "PAG": "Pagani",
    "PEU": "Peugeot",
    "PLY": "Plymouth",
    "POR": "Porsche",
    "REN": "Renault",
    "SUB": "Subaru",
    "TOY": "Toyota",
    "TVR": "TVR",
    "VW": "Volkswagen",
}

FRIENDLY_CAR_OVERRIDES = {
    "FER_F142_10": ("Ferrari", "458 Italia", 2010),
}


@dataclass(frozen=True)
class CarDisplayInfo:
    path: Path
    make: str
    model: str
    year: int | None

    @property
    def asset_id(self) -> str:
        return self.path.stem

    @property
    def year_label(self) -> str:
        return str(self.year) if self.year is not None else ""

    @property
    def friendly_name(self) -> str:
        parts = [self.make, self.model, self.year_label]
        return " ".join(part for part in parts if part).strip() or self.asset_id

    @property
    def search_text(self) -> str:
        return " ".join((self.friendly_name, self.asset_id, self.make, self.model)).upper()


def _friendly_model_name(raw_model: str) -> str:
    if not raw_model:
        return ""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Z][a-z])", " ", raw_model)
    return spaced.replace("_", " ").strip()


def _asset_year(two_digit_year: str) -> int | None:
    if not two_digit_year.isdigit():
        return None
    year = int(two_digit_year)
    # Filename suffixes are only a fallback; the readable GameDB supplies the
    # displayed year. Keep historical cars in the 1900s without that database.
    return 2000 + year if year < 30 else 1900 + year


def car_display_info(zip_path: Path) -> CarDisplayInfo:
    override = FRIENDLY_CAR_OVERRIDES.get(zip_path.stem.upper())
    if override is not None:
        make, model, year = override
        return CarDisplayInfo(zip_path, make, model, year)

    parts = zip_path.stem.split("_")
    prefix = parts[0].upper() if parts else ""
    make = MANUFACTURER_PREFIXES.get(prefix, prefix.title() if prefix else "")
    year = _asset_year(parts[-1]) if len(parts) >= 3 else None
    model_parts = parts[1:-1] if year is not None else parts[1:]
    model = _friendly_model_name(" ".join(model_parts))
    return CarDisplayInfo(zip_path, make, model, year)


def _game_db_car_years() -> dict[str, int]:
    """Use source years for catalog labels when the matching GameDB is readable."""
    game_db_path = _readable_game_db_path()
    if game_db_path is None:
        return {}
    try:
        with closing(sqlite3.connect(game_db_path.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute("SELECT MediaName, Year FROM Data_Car").fetchall()
    except (OSError, sqlite3.DatabaseError):
        return {}
    return {
        media_name.upper(): year
        for media_name, year in rows
        if isinstance(media_name, str) and isinstance(year, int) and 1800 <= year <= 9999
    }


def get_car_display_infos() -> list[CarDisplayInfo]:
    db_years = _game_db_car_years()
    cars = []
    for zip_path in get_car_zips():
        info = car_display_info(zip_path)
        cars.append(CarDisplayInfo(info.path, info.make, info.model, db_years.get(info.asset_id.upper(), info.year)))
    return cars


_TEXTURE_EXTRACT_VERSION = 11
_TAG_GRUB = 0x47727562  # burG
_TAG_TXCH = 0x54584348
_TAG_TXCB = 0x54584342


@dataclass(frozen=True)
class _DxgiLayout:
    name: str
    block_bytes: int | None = None
    bytes_per_pixel: int | None = None


@dataclass(frozen=True)
class _SwatchTextureInfo:
    width: int
    height: int
    mip_count: int
    dxgi_format: int
    format_name: str
    encoding: int
    transcoding: int
    color_profile: int
    linear_size: int
    pixel_data: bytes


_DXGI_LAYOUTS: dict[int, _DxgiLayout] = {
    28: _DxgiLayout("R8G8B8A8_UNORM", bytes_per_pixel=4),
    29: _DxgiLayout("R8G8B8A8_UNORM_SRGB", bytes_per_pixel=4),
    71: _DxgiLayout("BC1_UNORM", block_bytes=8),
    72: _DxgiLayout("BC1_UNORM_SRGB", block_bytes=8),
    74: _DxgiLayout("BC2_UNORM", block_bytes=16),
    75: _DxgiLayout("BC2_UNORM_SRGB", block_bytes=16),
    77: _DxgiLayout("BC3_UNORM", block_bytes=16),
    78: _DxgiLayout("BC3_UNORM_SRGB", block_bytes=16),
    80: _DxgiLayout("BC4_UNORM", block_bytes=8),
    81: _DxgiLayout("BC4_SNORM", block_bytes=8),
    83: _DxgiLayout("BC5_UNORM", block_bytes=16),
    84: _DxgiLayout("BC5_SNORM", block_bytes=16),
    95: _DxgiLayout("BC6H_UF16", block_bytes=16),
    96: _DxgiLayout("BC6H_SF16", block_bytes=16),
    98: _DxgiLayout("BC7_UNORM", block_bytes=16),
    99: _DxgiLayout("BC7_UNORM_SRGB", block_bytes=16),
}
_PIL_DDS_DXGI_FALLBACKS = {
    # Pillow's DDS reader handles the linear layout; color space is applied in
    # Blender nodes, so these are safe header-only compatibility fallbacks.
    29: 28,
    72: 71,
    75: 74,
    78: 77,
    81: 80,
}


def _read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _dxgi_from_swatch_encoding(encoding: int, transcoding: int, color_profile: int) -> int:
    """Use the same TXCH encoding table as ForzaTech-extraction-tools."""
    format_encoded = encoding if transcoding <= 1 else transcoding - 2
    match format_encoded:
        case 0:
            return 72 if color_profile else 71
        case 1:
            return 75 if color_profile else 74
        case 2:
            return 78 if color_profile else 77
        case 3:
            return 80
        case 4:
            return 81
        case 5:
            return 83
        case 6:
            return 84
        case 7:
            return 95
        case 8:
            return 96
        case 9:
            return 99 if color_profile else 98
        case 13:
            return 29 if color_profile else 28
        case _:
            return 0


def _parse_swatchbin_texture_info(data: bytes) -> _SwatchTextureInfo:
    """Return texture metadata from TXCH plus the TXCB pixel payload."""
    try:
        tag = _read_u32(data, 0)
        if tag != _TAG_GRUB:
            raise ValueError("not a burG bundle")

        offset = 4
        major = data[offset]
        minor = data[offset + 1]
        offset += 2
        blob_count = _read_u16(data, offset)
        offset += 2
        offset += 8
        if major > 1 or (major == 1 and minor >= 1):
            blob_count = _read_u32(data, offset)
            offset += 4

        for _ in range(blob_count):
            blob_tag = _read_u32(data, offset)
            offset += 4
            offset += 2  # blob version
            metadata_count = _read_u16(data, offset)
            offset += 2
            metadata_offset = _read_u32(data, offset)
            offset += 4
            data_offset = _read_u32(data, offset)
            offset += 4
            data_size = _read_u32(data, offset)
            offset += 4
            offset += 4

            if blob_tag != _TAG_TXCB:
                continue

            txch = None
            for index in range(metadata_count):
                record_offset = metadata_offset + index * 8
                metadata_tag = _read_u32(data, record_offset)
                version_and_size = _read_u16(data, record_offset + 4)
                metadata_size = version_and_size >> 4
                relative_offset = _read_u16(data, record_offset + 6)
                if metadata_tag == _TAG_TXCH:
                    start = record_offset + relative_offset
                    txch = data[start:start + metadata_size]
                    break

            if txch is None:
                raise ValueError("TXCB blob has no TXCH metadata")
            if len(txch) < 80:
                raise ValueError("TXCH metadata is too short")

            width = _read_u32(txch, 24)
            height = _read_u32(txch, 28)
            mip_count = max(1, txch[38])
            transcoding = _read_u32(txch, 40)
            color_profile = _read_u32(txch, 48)
            encoding = _read_u32(txch, 64)
            linear_size = _read_u32(txch, 76)
            dxgi_format = _dxgi_from_swatch_encoding(encoding, transcoding, color_profile)
            layout = _DXGI_LAYOUTS.get(dxgi_format)
            format_name = layout.name if layout else f"UNKNOWN_{dxgi_format}"
            pixel_data = data[data_offset:data_offset + data_size]

            return _SwatchTextureInfo(
                width=width,
                height=height,
                mip_count=mip_count,
                dxgi_format=dxgi_format,
                format_name=format_name,
                encoding=encoding,
                transcoding=transcoding,
                color_profile=color_profile,
                linear_size=linear_size,
                pixel_data=pixel_data,
            )

        raise ValueError("no TXCB blob found")
    except Exception:
        # Legacy fallback for older experiments. Current FH swatchbins should
        # always take the TXCH path above.
        hcxt_offset = 0x2C
        pixel_data_offset = _read_u32(data, 0x08)
        width = _read_u32(data, hcxt_offset + 32)
        height = _read_u32(data, hcxt_offset + 36)
        fmt_word = _read_u32(data, hcxt_offset + 44)
        mip_count = max(1, (fmt_word >> 16) & 0xFF)
        return _SwatchTextureInfo(
            width=width,
            height=height,
            mip_count=mip_count,
            dxgi_format=99,
            format_name=_DXGI_LAYOUTS[99].name,
            encoding=9,
            transcoding=0,
            color_profile=1,
            linear_size=max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16,
            pixel_data=data[pixel_data_offset:],
        )


def _parse_swatchbin_header(data: bytes) -> tuple[int, int, int, bytes]:
    """Compatibility wrapper returning width, height, mips, and TXCB payload."""
    info = _parse_swatchbin_texture_info(data)
    return info.width, info.height, info.mip_count, info.pixel_data


def _expected_texture_chain_size(width: int, height: int, mip_count: int, dxgi_format: int) -> int:
    """Compute the byte size of one mip chain for the swatch's DXGI format."""
    layout = _DXGI_LAYOUTS.get(dxgi_format)
    if layout is None:
        return 0

    total = 0
    for level in range(max(1, mip_count)):
        mip_width = max(1, width >> level)
        mip_height = max(1, height >> level)
        if layout.block_bytes is not None:
            total += max(1, (mip_width + 3) // 4) * max(1, (mip_height + 3) // 4) * layout.block_bytes
        elif layout.bytes_per_pixel is not None:
            total += mip_width * mip_height * layout.bytes_per_pixel
    return total


def _slice_texture_payload(
    pixel_data: bytes,
    width: int,
    height: int,
    mip_count: int,
    dxgi_format: int,
    *,
    slice_index: int = 0,
) -> bytes:
    """Return one validated texture slice from a TXCB payload."""
    chain_size = _expected_texture_chain_size(width, height, mip_count, dxgi_format)
    if chain_size <= 0:
        raise ValueError(f"unsupported DXGI format {dxgi_format}")
    if len(pixel_data) < chain_size:
        raise ValueError(f"payload too small for DXGI {dxgi_format}: {len(pixel_data)} < {chain_size}")
    if len(pixel_data) == chain_size:
        return pixel_data

    slice_count = max(1, len(pixel_data) // chain_size)
    safe_index = max(0, min(slice_index, slice_count - 1))
    start = safe_index * chain_size
    return pixel_data[start:start + chain_size]


def _texture_payload_is_valid(info: _SwatchTextureInfo) -> bool:
    expected = _expected_texture_chain_size(info.width, info.height, info.mip_count, info.dxgi_format)
    return expected > 0 and len(info.pixel_data) >= expected and len(info.pixel_data) % expected == 0


def _swatchbin_to_intermediate_dds(
    data: bytes,
    *,
    dxgi_format: int | None = None,
    slice_index: int = 0,
) -> bytes:
    """Convert a burG swatchbin texture container to a metadata-correct DDS."""
    info = _parse_swatchbin_texture_info(data)
    effective_dxgi = info.dxgi_format if dxgi_format is None else dxgi_format
    layout = _DXGI_LAYOUTS.get(effective_dxgi)
    if layout is None:
        raise ValueError(f"unsupported DXGI format {effective_dxgi}")

    pixel_data = _slice_texture_payload(
        info.pixel_data,
        info.width,
        info.height,
        info.mip_count,
        effective_dxgi,
        slice_index=slice_index,
    )

    DDSD_CAPS = 0x1
    DDSD_HEIGHT = 0x2
    DDSD_WIDTH = 0x4
    DDSD_PITCH = 0x8
    DDSD_PIXELFORMAT = 0x1000
    DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT

    if layout.block_bytes is not None:
        flags |= DDSD_LINEARSIZE
        pitch_or_linear_size = max(1, (info.width + 3) // 4) * max(1, (info.height + 3) // 4) * layout.block_bytes
    else:
        flags |= DDSD_PITCH
        pitch_or_linear_size = info.width * (layout.bytes_per_pixel or 4)

    DDSCAPS_COMPLEX = 0x8
    DDSCAPS_MIPMAP = 0x400000
    DDSCAPS_TEXTURE = 0x1000
    caps = DDSCAPS_TEXTURE
    if info.mip_count > 1:
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP

    header = struct.pack(
        "<7I",
        124,
        flags,
        info.height,
        info.width,
        pitch_or_linear_size,
        0,
        info.mip_count,
    )
    header += bytes(44)
    header += struct.pack("<2I4sI4I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    header += struct.pack("<5I", caps, 0, 0, 0, 0)
    dds_dxgi = _PIL_DDS_DXGI_FALLBACKS.get(effective_dxgi, effective_dxgi)
    dx10 = struct.pack("<5I", dds_dxgi, 3, 0, 1, 0)

    return b"DDS " + header + dx10 + pixel_data


_NORMAL_Z_LUT: bytes | None = None


def _get_normal_z_lut() -> bytes:
    global _NORMAL_Z_LUT
    if _NORMAL_Z_LUT is None:
        values = []
        for r in range(256):
            nx = (r / 255.0) * 2.0 - 1.0
            for g in range(256):
                ny = (g / 255.0) * 2.0 - 1.0
                nz = math.sqrt(max(0.0, 1.0 - (nx * nx) - (ny * ny)))
                values.append(int((nz * 0.5 + 0.5) * 255.0))
        _NORMAL_Z_LUT = bytes(values)
    return _NORMAL_Z_LUT


def _reconstruct_normal_from_bc5(decoded_bc5: Image.Image) -> Image.Image:
    """Rebuild Z from BC5 XY channels and return a standard tangent-space normal."""
    rgb = decoded_bc5.convert("RGB")
    red, green, _ = rgb.split()
    lut = _get_normal_z_lut()
    z_bytes = bytes(lut[(r << 8) | g] for r, g in zip(red.tobytes(), green.tobytes()))
    blue = Image.frombytes("L", rgb.size, z_bytes)
    alpha = Image.new("L", rgb.size, 255)
    return Image.merge("RGBA", (red, green, blue, alpha))


def _decode_ao_texture(data: bytes) -> Image.Image:
    """Decode AO as single-channel grayscale with no alpha output."""
    info = _parse_swatchbin_texture_info(data)
    img = Image.open(BytesIO(_swatchbin_to_intermediate_dds(data))).convert("RGBA")
    if info.dxgi_format in {80, 81}:
        return img.convert("L")
    return img.split()[3].convert("L")


def _decode_normal_texture(data: bytes) -> Image.Image:
    """Decode normals according to TXCH DXGI metadata."""
    info = _parse_swatchbin_texture_info(data)
    img = Image.open(BytesIO(_swatchbin_to_intermediate_dds(data))).convert("RGBA")
    if info.dxgi_format in {83, 84}:
        return _reconstruct_normal_from_bc5(img)
    return img


def _decode_channel_image(data: bytes, channel: str) -> Image.Image:
    """Decode swatch data into a display-ready image based on channel semantics."""
    if channel in {"nrml", "normal"}:
        return _decode_normal_texture(data)
    if channel == "ao":
        return _decode_ao_texture(data)
    return Image.open(BytesIO(_swatchbin_to_intermediate_dds(data))).convert("RGBA")


def _mark_tiff_extra_samples_unspecified(path: Path) -> bool:
    """
    Mark RGBA TIFF extra samples as data channels instead of transparency.

    Pillow writes RGBA TIFFs with ExtraSamples=2 (unassociated alpha). That is
    semantically correct for diffuse transparency, but normal-map alpha is a
    packed data channel. Photoshop treats ExtraSamples=2 as layer transparency,
    which hides the channel and can make the RGB appear premultiplied. Setting
    ExtraSamples=0 keeps the fourth sample in the file without declaring it as
    alpha.
    """
    data = bytearray(path.read_bytes())
    if len(data) < 8:
        return False

    if data[:2] == b"II":
        endian = "<"
    elif data[:2] == b"MM":
        endian = ">"
    else:
        return False

    magic = struct.unpack_from(endian + "H", data, 2)[0]
    changed = False

    if magic == 42:
        ifd_offset = struct.unpack_from(endian + "I", data, 4)[0]
        while ifd_offset:
            entry_count = struct.unpack_from(endian + "H", data, ifd_offset)[0]
            for i in range(entry_count):
                entry_offset = ifd_offset + 2 + (i * 12)
                tag, field_type, count = struct.unpack_from(endian + "HHI", data, entry_offset)
                if tag != 338 or field_type != 3:
                    continue
                value_size = count * 2
                value_offset = entry_offset + 8 if value_size <= 4 else struct.unpack_from(endian + "I", data, entry_offset + 8)[0]
                for sample_index in range(count):
                    current = struct.unpack_from(endian + "H", data, value_offset + (sample_index * 2))[0]
                    if current != 0:
                        struct.pack_into(endian + "H", data, value_offset + (sample_index * 2), 0)
                        changed = True
            next_offset_pos = ifd_offset + 2 + (entry_count * 12)
            ifd_offset = struct.unpack_from(endian + "I", data, next_offset_pos)[0]
    elif magic == 43:
        if len(data) < 16:
            return False
        ifd_offset = struct.unpack_from(endian + "Q", data, 8)[0]
        while ifd_offset:
            entry_count = struct.unpack_from(endian + "Q", data, ifd_offset)[0]
            for i in range(entry_count):
                entry_offset = ifd_offset + 8 + (i * 20)
                tag, field_type = struct.unpack_from(endian + "HH", data, entry_offset)
                count = struct.unpack_from(endian + "Q", data, entry_offset + 4)[0]
                if tag != 338 or field_type != 3:
                    continue
                value_size = count * 2
                value_offset = entry_offset + 12 if value_size <= 8 else struct.unpack_from(endian + "Q", data, entry_offset + 12)[0]
                for sample_index in range(count):
                    current = struct.unpack_from(endian + "H", data, value_offset + (sample_index * 2))[0]
                    if current != 0:
                        struct.pack_into(endian + "H", data, value_offset + (sample_index * 2), 0)
                        changed = True
            next_offset_pos = ifd_offset + 8 + (entry_count * 20)
            ifd_offset = struct.unpack_from(endian + "Q", data, next_offset_pos)[0]
    else:
        return False

    if changed:
        path.write_bytes(data)
    return changed


def _repair_normal_texture_tiff_metadata(texture_map: dict[str, Path]) -> None:
    for key, path in texture_map.items():
        if key.endswith(("__nrml", "__normal")) and path.exists():
            _mark_tiff_extra_samples_unspecified(path)


def _decode_and_save_texture(data: bytes, channel: str, out_path: Path) -> _SwatchTextureInfo:
    info = _parse_swatchbin_texture_info(data)
    img = _decode_channel_image(data, channel)
    img.save(out_path, format="TIFF", compression="tiff_lzw")
    if channel in {"nrml", "normal"} and "A" in img.getbands():
        _mark_tiff_extra_samples_unspecified(out_path)
    return info


def _texture_manifest_path(tex_dir: Path) -> Path:
    return tex_dir / ".extract_manifest.json"


def _zip_signature(zip_path: Path) -> dict[str, object]:
    stat = zip_path.stat()
    return {
        "source": str(zip_path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "extractor_version": _TEXTURE_EXTRACT_VERSION,
    }


def _load_texture_manifest(zip_path: Path, tex_dir: Path) -> dict[str, Path] | None:
    manifest_path = _texture_manifest_path(tex_dir)
    if not manifest_path.exists():
        return None

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("signature") != _zip_signature(zip_path):
            return None
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return None

        texture_map: dict[str, Path] = {}
        for key, filename in entries.items():
            path = tex_dir / str(filename)
            if path.exists():
                texture_map[str(key)] = path

        return texture_map if texture_map else None
    except Exception:
        return None


def _save_texture_manifest(zip_path: Path, tex_dir: Path, texture_map: dict[str, Path]) -> None:
    manifest_path = _texture_manifest_path(tex_dir)
    entries = {key: path.name for key, path in texture_map.items()}
    payload = {
        "signature": _zip_signature(zip_path),
        "entries": entries,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


_shared_textures_cache: dict[str, Path] | None = None
_shared_textures_lock = threading.Lock()
_SWATCH_CHANNEL_PATTERN = re.compile(
    r"_(basecoloralpha|nrml|normal|glos|rmao|ao|alpha|mask\d*|lite|diffuse)_",
    flags=re.IGNORECASE,
)
_shared_materials_extracted: set[str] = set()
_shared_tire_models_extracted: set[tuple[str, str]] = set()
_shared_library_extract_lock = threading.Lock()


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for info in zf.infolist():
        destination = (target_dir / info.filename).resolve()
        if destination != target_root and target_root not in destination.parents:
            raise ValueError(f"Refusing to extract zip entry outside target directory: {info.filename}")
    zf.extractall(target_dir)


def _ensure_shared_material_library_extracted(workspace: Path = WORKSPACE) -> None:
    """Extract shared ``Materials.zip`` into workspace ``Media/cars/_library``.

    carbin_importer resolves parent materialbin paths as loose files. FH stores
    them in ``cars/_library/Materials.zip``, so we unpack this archive once to
    make data-driven shader/material parent resolution work.
    """
    target_dir = workspace / "Media" / "cars" / "_library"
    cache_key = str(target_dir.resolve())
    if cache_key in _shared_materials_extracted:
        return

    with _shared_library_extract_lock:
        if cache_key in _shared_materials_extracted:
            return
        if not SHARED_MATERIALS_ZIP.exists():
            _shared_materials_extracted.add(cache_key)
            return

        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(SHARED_MATERIALS_ZIP, "r") as zf:
            _safe_extract_zip(zf, target_dir)

        _shared_materials_extracted.add(cache_key)


def _normalize_tire_model_name(value: str | None) -> str:
    tire_model_name = (value or "").strip()
    if (
        not tire_model_name
        or tire_model_name in {".", ".."}
        or any(char in tire_model_name for char in '/\\:*?"<>|')
        or any(ord(char) < 32 for char in tire_model_name)
    ):
        return DEFAULT_TIRE_MODEL_NAME
    return tire_model_name


def _stock_tire_import_spec_from_db(media_name: str) -> TireImportSpec | None:
    """Read one complete stock import specification for an exact car asset."""
    game_db_path = _readable_game_db_path()
    if game_db_path is None:
        return None

    try:
        with closing(sqlite3.connect(game_db_path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                """
                SELECT tire.TireModelName,
                       car.FrontTireWidthMM, car.FrontTireAspect, car.FrontWheelDiameterIN,
                       car.RearTireWidthMM, car.RearTireAspect, car.RearWheelDiameterIN,
                       geometry.ModelWheelbase,
                       geometry.ModelFrontTrackOuter, geometry.ModelRearTrackOuter,
                       geometry.BottomCenterWheelbasePosx,
                       geometry.BottomCenterWheelbasePosy,
                       geometry.BottomCenterWheelbasePosZ,
                       geometry.ModelFrontStockRideHeight,
                       geometry.ModelRearStockRideHeight
                FROM Data_Car AS car
                    INNER JOIN List_UpgradeTireCompound AS tire
                        ON tire.Ordinal = car.Id AND tire.IsStock = 1
                    INNER JOIN List_UpgradeCarBody AS body
                        ON body.Ordinal = car.Id AND body.IsStock = 1
                    INNER JOIN Data_CarBody AS geometry ON geometry.Id = body.CarBodyID
                WHERE car.MediaName = ? COLLATE NOCASE
                ORDER BY body.CarBodyID
                LIMIT 1
                """,
                (media_name,),
            ).fetchone()
    except (OSError, sqlite3.DatabaseError):
        return None

    if row is None or not isinstance(row[0], str) or not row[0].strip():
        return None
    tire_name = row[0].strip()
    if _normalize_tire_model_name(tire_name) != tire_name:
        return None
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in row[1:7]):
        return None
    dimensions = row[1:7]
    try:
        geometry = tuple(float(value) for value in row[7:])
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(value) for value in geometry):
        return None
    if any(value <= 0 for value in (*geometry[:3], *geometry[6:])):
        return None
    return TireImportSpec(tire_name, *dimensions, *geometry)


def _stock_tire_model_name_from_db(media_name: str) -> str | None:
    spec = _stock_tire_import_spec_from_db(media_name)
    return spec.tire_model_name if spec is not None else None


def _tire_spec_override(media_name: str) -> TireImportSpec | None:
    return CAR_TIRE_SPEC_OVERRIDES.get(media_name.upper())


def _fallback_tire_import_spec(media_name: str) -> TireImportSpec:
    return _tire_spec_override(media_name) or DEFAULT_TIRE_IMPORT_SPEC


def _tire_model_name_for_import(media_name: str) -> tuple[str, str]:
    tire_model_name = _stock_tire_model_name_from_db(media_name)
    if tire_model_name is not None:
        return tire_model_name, "GameDB"
    override = _tire_spec_override(media_name)
    if override is not None:
        return _normalize_tire_model_name(override.tire_model_name), "override"
    return DEFAULT_TIRE_MODEL_NAME, "default"


def _ensure_shared_tire_model_extracted(
    media_name: str,
    workspace: Path = WORKSPACE,
    log: Callable[[str], None] | None = None,
) -> str:
    tire_model_name, source = _tire_model_name_for_import(media_name)
    tire_model_name = _normalize_tire_model_name(tire_model_name)
    tire_folder_name = f"tire_{tire_model_name}"
    target_dir = workspace / "Media" / "Cars" / "_library" / "scene" / "tires" / tire_folder_name
    target_model = target_dir / f"tireL_{tire_model_name}.modelbin"
    cache_key = (str(target_dir.resolve()), tire_model_name.lower())
    source_zip = SHARED_TIRE_ZIP_DIR / f"{tire_folder_name}.zip"

    if target_model.exists():
        _shared_tire_models_extracted.add(cache_key)
        if log:
            log(f"Shared tire mesh ready: {tire_folder_name} ({source}, cached)")
        return tire_model_name

    with _shared_library_extract_lock:
        if cache_key in _shared_tire_models_extracted and target_model.exists():
            if log:
                log(f"Shared tire mesh ready: {tire_folder_name} ({source}, cached)")
            return tire_model_name
        if not source_zip.exists():
            if log:
                log(f"Shared tire zip not found: {source_zip.name}")
            _shared_tire_models_extracted.add(cache_key)
            return tire_model_name

        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source_zip, "r") as zf:
            _safe_extract_zip(zf, target_dir)

        if log:
            log(f"Shared tire mesh ready: {tire_folder_name} ({source})")
        _shared_tire_models_extracted.add(cache_key)

    return tire_model_name


def _get_shared_textures(log: Callable[[str], None] | None = None) -> dict[str, Path]:
    """
    Extract shared tiling/material textures from ``cars/_library/Textures.zip``
    on the first call and return the cached map on subsequent calls.

    Shared textures include carbon fiber, alcantara, leather, suede, fabric,
    wood, and other tiling materials used across all cars.  They live in the
    ``_fmnext/`` folder of the library zip and follow the same
    ``Name_BaseColorAlpha_hash.swatchbin`` naming convention as car-specific
    textures.

    Returns an empty dict if the library zip does not exist (non-fatal).
    """
    global _shared_textures_cache
    if _shared_textures_cache is not None:
        return _shared_textures_cache
    with _shared_textures_lock:
        if _shared_textures_cache is not None:
            return _shared_textures_cache
        if not SHARED_TEXTURES_ZIP.exists():
            _shared_textures_cache = {}
            return _shared_textures_cache
        _shared_textures_cache = extract_textures(SHARED_TEXTURES_ZIP, SHARED_TEX_DIR, log=log)
        return _shared_textures_cache


def extract_textures(
    zip_path: Path,
    tex_dir: Path,
    log: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    """
    Extract supported swatchbin texture channels from a zip, convert each to
    DDS, and save them in *tex_dir*.

    Returns a lookup with these key forms:
    - ``base_name`` for diffuse ``_BaseColorAlpha_`` entries
    - ``base_name__<channel>`` for non-diffuse channels
      (e.g. ``"carbonfiber_001__nrml"``, ``"carbonfiber_001__rmao"``)

    .. note::
        Channels are inferred from the marker in
        ``{Name}_{Channel}_{hash}.swatchbin`` where Channel is one of
        BaseColorAlpha/NRML/Normal/GLOS/RMAO/AO/Alpha/Mask/Mask2/Lite/Diffuse.
    """
    tex_dir.mkdir(parents=True, exist_ok=True)

    cached_map = _load_texture_manifest(zip_path, tex_dir)
    if cached_map is not None:
        _repair_normal_texture_tiff_metadata(cached_map)
        if log:
            log(f"[textures] {zip_path.name}: cache hit, {len(cached_map)} texture(s)")
        return cached_map

    started = time.perf_counter()
    texture_map: dict[str, Path] = {}
    best_entry_score: dict[str, tuple[int, int, int]] = {}
    selected_entries: dict[str, tuple[str, str, Path, _SwatchTextureInfo]] = {}
    scanned = 0
    skipped_invalid = 0
    failures = 0
    failure_examples: list[str] = []
    format_counts: dict[str, int] = {}

    with zipfile.ZipFile(zip_path, "r") as zf:
        for entry in zf.namelist():
            lower = entry.lower()
            if not lower.endswith(".swatchbin"):
                continue
            match = _SWATCH_CHANNEL_PATTERN.search(lower)
            if not match:
                continue
            scanned += 1

            channel = match.group(1).lower()
            try:
                data = zf.read(entry)
            except Exception:
                failures += 1
                if len(failure_examples) < 5:
                    failure_examples.append(f"{entry}: read failed")
                continue

            # Derive base name from the last path component
            filename = Path(entry).name
            parts = _SWATCH_CHANNEL_PATTERN.split(filename, maxsplit=1)
            if len(parts) < 3:
                continue
            base_name = parts[0].lower()

            if channel == "basecoloralpha":
                key = base_name
                out_name = f"{base_name}.tif"
            else:
                key = f"{base_name}__{channel}"
                out_name = f"{key}.tif"

            out_path = tex_dir / out_name
            try:
                info = _parse_swatchbin_texture_info(data)
                if not _texture_payload_is_valid(info):
                    skipped_invalid += 1
                    if len(failure_examples) < 5:
                        expected = _expected_texture_chain_size(
                            info.width,
                            info.height,
                            info.mip_count,
                            info.dxgi_format,
                        )
                        failure_examples.append(
                            f"{entry}: {info.format_name} payload {len(info.pixel_data)} != expected multiple of {expected}"
                        )
                    continue
                payload_size = len(info.pixel_data)
                candidate_score = (info.width * info.height, info.mip_count, payload_size)

                # Some channels have multiple swatch variants with identical
                # base/channel names. Keep the highest-detail variant.
                previous_score = best_entry_score.get(key)
                if previous_score is not None and candidate_score < previous_score:
                    continue
                best_entry_score[key] = candidate_score
                selected_entries[key] = (entry, channel, out_path, info)
            except Exception as exc:
                failures += 1
                if len(failure_examples) < 5:
                    failure_examples.append(f"{entry}: metadata failed ({exc})")
                continue

    with zipfile.ZipFile(zip_path, "r") as zf:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_map = {}
            for key, (entry, channel, out_path, _) in selected_entries.items():
                try:
                    data = zf.read(entry)
                except Exception:
                    failures += 1
                    if len(failure_examples) < 5:
                        failure_examples.append(f"{entry}: read failed")
                    continue
                future = executor.submit(_decode_and_save_texture, data, channel, out_path)
                future_map[future] = (key, out_path)

            for future in as_completed(future_map):
                key, out_path = future_map[future]
                try:
                    info = future.result()
                    texture_map[key] = out_path
                    format_counts[info.format_name] = format_counts.get(info.format_name, 0) + 1
                except Exception as exc:
                    failures += 1
                    if len(failure_examples) < 5:
                        failure_examples.append(f"{key}: decode failed ({exc})")
                    continue

    _save_texture_manifest(zip_path, tex_dir, texture_map)

    if log:
        elapsed = max(0.001, time.perf_counter() - started)
        rate = len(texture_map) / elapsed
        format_text = ", ".join(
            f"{name}={count}"
            for name, count in sorted(format_counts.items(), key=lambda item: (-item[1], item[0]))
        ) or "none"
        log(
            f"[textures] {zip_path.name}: selected {len(selected_entries)} from {scanned} swatchbin(s), "
            f"decoded {len(texture_map)} in {elapsed:.1f}s ({rate:.1f}/s), formats: {format_text}"
        )
        if skipped_invalid or failures:
            log(f"[textures] {zip_path.name}: skipped_invalid={skipped_invalid}, failures={failures}")
            for example in failure_examples:
                log(f"    [texture] {example}")

    return texture_map


def extract_car(zip_path: Path, workspace: Path) -> str:
    media_name = zip_path.stem
    car_dir = workspace / "Media" / "Cars" / media_name
    car_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        _safe_extract_zip(zf, car_dir)
    return media_name


def _set_importer_setting(script: str, name: str, value: object) -> str:
    """Fail visibly if an upstream importer setting moves or is duplicated."""
    updated, count = re.subn(
        rf"^{re.escape(name)}\s*=.*$",
        lambda _: f"{name} = {value}",
        script,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError(f"Expected one {name} setting in carbin importer, found {count}")
    return updated


def generate_blender_script(game_path: str, media_name: str, body_only: bool = False, interior_only: bool = False) -> str:
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    safe_path = game_path.replace("\\", "/")
    fallback_path = str(FORZA_PATH).replace("\\", "/")
    game_db_path = _readable_game_db_path()
    use_game_db = game_db_path is not None and _stock_tire_import_spec_from_db(media_name) is not None
    suspension_transform_type = 2 if use_game_db else 1
    db_path_literal = str(game_db_path or (FORZA_PATH / "media" / "stripped" / "gamedbRC.slt")).replace("\\", "/")
    fallback_tire_spec = _fallback_tire_import_spec(media_name)
    fallback_tire_model_name = _normalize_tire_model_name(fallback_tire_spec.tire_model_name)

    script = _set_importer_setting(script, "game_path", repr(safe_path))
    game_path_setting = f"game_path = {safe_path!r}\n"
    if script.count(game_path_setting) != 1:
        raise ValueError("Cannot insert carbin importer fallback path")
    script = script.replace(
        game_path_setting,
        game_path_setting + f"game_path_fallback = {fallback_path!r}\n",
        1,
    )
    script = _set_importer_setting(script, "media_name", repr(media_name))
    script = _set_importer_setting(script, "db_path", repr(db_path_literal))
    script = _set_importer_setting(script, "use_db", use_game_db)
    script = _set_importer_setting(script, "suspension_transform_type", suspension_transform_type)
    script = _set_importer_setting(script, "quadrangulate_mesh", True)
    script = _set_importer_setting(script, "use_materials", True)
    script = _set_importer_setting(script, "shader_processor", 1)

    # Patch: resolve GAME: paths against workspace extraction first, then
    # installed Forza content root for shared _library assets.
    script = script.replace(
        '            return self.root + path[5:] # TODO: support directories without media\\cars\\\n',
        '            tail = path[5:]\n'
        '            candidates = [self.root + tail]\n'
        '            if game_path_fallback:\n'
        '                candidates.append(game_path_fallback + tail)\n'
        '            for raw_candidate in candidates:\n'
        '                candidate = os.path.normpath(raw_candidate)\n'
        '                variants = [candidate]\n'
        '                lowered = candidate.lower().replace("/", "\\\\")\n'
        '                separator = chr(92)\n'
        '                library_marker = separator + separator.join(("media", "cars", "_library")) + separator\n'
        '                marker = library_marker + "materials" + separator\n'
        '                marker_index = lowered.find(marker)\n'
        '                if marker_index != -1:\n'
        '                    remove_start = marker_index + len(library_marker)\n'
        '                    remove_end = remove_start + len("materials" + separator)\n'
        '                    variants.append(candidate[:remove_start] + candidate[remove_end:])\n'
        '                for variant in variants:\n'
        '                    if os.path.exists(variant):\n'
        '                        return variant\n'
        '            return os.path.normpath(candidates[0])\n',
        1,
    )

    # Keep compatibility values for cars without a complete stock database row.
    fallback_anchor = 'use_db = False\n'
    if not use_game_db and script.count(fallback_anchor) != 1:
        raise ValueError("Cannot insert fallback tire and placement settings")
    script = script.replace(
        fallback_anchor,
        fallback_anchor +
        f'TireModelName = "{fallback_tire_model_name}"\n'
        f'FrontTireWidthMM = {fallback_tire_spec.front_width_mm}\n'
        f'OriginalFrontTireAspect = {fallback_tire_spec.front_aspect}\n'
        f'OriginalFrontWheelDiameterIN = {fallback_tire_spec.front_wheel_diameter_in}\n'
        'FrontWheelDiameterIN = OriginalFrontWheelDiameterIN\n'
        f'RearTireWidthMM = {fallback_tire_spec.rear_width_mm}\n'
        f'OriginalRearTireAspect = {fallback_tire_spec.rear_aspect}\n'
        f'OriginalRearWheelDiameterIN = {fallback_tire_spec.rear_wheel_diameter_in}\n'
        'RearWheelDiameterIN = OriginalRearWheelDiameterIN\n'
        f'ModelWheelbase = {fallback_tire_spec.model_wheelbase}\n'
        f'ModelFrontTrackOuter = {fallback_tire_spec.model_front_track_outer}\n'
        f'ModelRearTrackOuter = {fallback_tire_spec.model_rear_track_outer}\n'
        f'BottomCenterWheelbasePosX = {fallback_tire_spec.bottom_center_wheelbase_pos_x}\n'
        f'BottomCenterWheelbasePosY = {fallback_tire_spec.bottom_center_wheelbase_pos_y}\n'
        f'BottomCenterWheelbasePosZ = {fallback_tire_spec.bottom_center_wheelbase_pos_z}\n'
        f'ModelFrontStockRideHeight = {fallback_tire_spec.model_front_stock_ride_height}\n'
        f'ModelRearStockRideHeight = {fallback_tire_spec.model_rear_stock_ride_height}\n',
        1,
    )

    # Patch: skip models whose files don't exist.
    script = script.replace(
        '        p = path_resolver.resolve(model.path)\n'
        '        print(p)\n'
        '        s = BinaryStream.from_path(p)\n',
        '        p = path_resolver.resolve(model.path)\n'
        '        print(p)\n'
        '        if not os.path.exists(p):\n'
        '            print(f"  [SKIP] File not found: {p}")\n'
        '            continue\n'
        '        s = BinaryStream.from_path(p)\n',
    )

    # Patch: missing FH6 material parents in _library should not abort the import.
    script = script.replace(
        '            # print("Material: " + f_path)\n'
        '            with open(f_path, "rb", 0) as f:\n'
        '                s = BinaryStream(f.read())\n'
        '            parent = MaterialSystemObject()\n'
        '            parent.deserialize(s)\n'
        '            self.shader_name = parent.shader_name\n'
        '            if self.shader_name is None:\n'
        '                self.shader_name = parent_blob.metadata[Tag.Name].read_string()\n'
        '            self.parameters = parent.parameters\n',
        '            # print("Material: " + f_path)\n'
        '            if not os.path.exists(f_path):\n'
        '                print(f"  [SKIP MATERIAL] File not found: {f_path}")\n'
        '                self.shader_name = parent_blob.metadata[Tag.Name].read_string()\n'
        '            else:\n'
        '                with open(f_path, "rb", 0) as f:\n'
        '                    s = BinaryStream(f.read())\n'
        '                parent = MaterialSystemObject()\n'
        '                parent.deserialize(s)\n'
        '                self.shader_name = parent.shader_name\n'
        '                if self.shader_name is None:\n'
        '                    self.shader_name = parent_blob.metadata[Tag.Name].read_string()\n'
        '                self.parameters = parent.parameters\n',
        1,
    )

    # Patch: guard tire_models access against None (tires skipped)
    script = script.replace(
        '    scene.part_tires.tire_models[wheel_index].modelbin.set_transform(wheel_model.modelbin.transform)',
        '    if scene.part_tires.tire_models[wheel_index] is not None:\n'
        '        scene.part_tires.tire_models[wheel_index].modelbin.set_transform(wheel_model.modelbin.transform)',
    )

    # Patch: add shader parameter names observed in the FH6 car_carbonfiber
    # shader/material data. The wrapper consumes these as metadata; it does not
    # ask the upstream importer to build a full carbon fiber material.
    script = script.replace(
        '    CH1GlossDiffMaskTexture = 0x022DF609\n',
        '    CH1GlossDiffMaskTexture = 0x022DF609\n'
        '\n'
        '    # car_carbonfiber\n'
        '    U_Tiling_floatVal = 0x19A7D8F1\n'
        '    V_Tiling_floatVal = 0x4A3D8375\n'
        '    WeaveNormal_Texture = 0xEC13FF23\n'
        '    WeaveMask_Texture = 0xBAA9FAA5\n'
        '    WeaveColorTintAColorParam = 0x29D1EC60\n'
        '    WeaveColorTintBColorParam = 0xB0338A61\n'
        '    WeaveNormal_Intensity_floatVal = 0xDF91836E\n',
        1,
    )
    script = script.replace(
        '    WeaveNormal_Intensity_floatVal = 0xDF91836E\n',
        '    WeaveNormal_Intensity_floatVal = 0xDF91836E\n'
        '\n'
        '    # FMNext standard PBR shaders: car_standard, car_standard_fabric,\n'
        '    # car_standard_coated, car_label, and related materials.\n'
        '    BaseColorAlpha_Texture = 0x85E937A9\n'
        '    RoughMetalAO_Texture = 0x8D9C56EF\n'
        '    Standard_Normal_Texture = 0xF9E8078D\n'
        '    Standard_Alpha_Texture = 0x698CA64F\n'
        '    Emissive_Lite_Texture = 0x6161E552\n'
        '    Emissive_Mask_Texture = 0x7F1965BF\n',
        1,
    )

    # Patch: expose data-driven material metadata on imported Blender objects.
    # This lets post-import fallback logic use exact importer-resolved links
    # instead of brittle substring matching on object names.
    script = script.replace(
        '            material_instance = modelbin.materials[mesh.material_id]\n',
        '            material_instance = modelbin.materials[mesh.material_id]\n'
        '            obj["forza_material_id"] = int(mesh.material_id)\n'
        '            obj["forza_material_name"] = material_instance.name\n'
        '            if hasattr(material_instance, "shader_name") and material_instance.shader_name is not None:\n'
        '                obj["forza_shader_name"] = material_instance.shader_name\n'
        '            _diffuse_tiling = getattr(material_instance, "diffuse_texture_tiling", (1, 1))\n'
        '            obj["forza_diffuse_texture_texcoord"] = str(getattr(material_instance, "diffuse_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_diffuse_texture_tiling"] = f"{float(_diffuse_tiling[0])},{float(_diffuse_tiling[1])}"\n'
        '            _normal_tiling = getattr(material_instance, "normal_texture_tiling", (1, 1))\n'
        '            obj["forza_normal_texture_texcoord"] = str(getattr(material_instance, "normal_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_normal_texture_tiling"] = f"{float(_normal_tiling[0])},{float(_normal_tiling[1])}"\n'
        '            _gloss_tiling = getattr(material_instance, "gloss_texture_tiling", (1, 1))\n'
        '            obj["forza_gloss_texture_texcoord"] = str(getattr(material_instance, "gloss_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_gloss_texture_tiling"] = f"{float(_gloss_tiling[0])},{float(_gloss_tiling[1])}"\n'
        '            _alpha_tiling = getattr(material_instance, "alpha_texture_tiling", (1, 1))\n'
        '            obj["forza_alpha_texture_texcoord"] = str(getattr(material_instance, "alpha_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_alpha_texture_tiling"] = f"{float(_alpha_tiling[0])},{float(_alpha_tiling[1])}"\n'
        '            _mask_tiling = getattr(material_instance, "mask_texture_tiling", (1, 1))\n'
        '            obj["forza_mask_texture_texcoord"] = str(getattr(material_instance, "mask_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_mask_texture_tiling"] = f"{float(_mask_tiling[0])},{float(_mask_tiling[1])}"\n'
        '            _emissive_tiling = getattr(material_instance, "emissive_texture_tiling", (1, 1))\n'
        '            obj["forza_emissive_texture_texcoord"] = str(getattr(material_instance, "emissive_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_emissive_texture_tiling"] = f"{float(_emissive_tiling[0])},{float(_emissive_tiling[1])}"\n'
        '            _emissive_mask_tiling = getattr(material_instance, "emissive_mask_texture_tiling", (1, 1))\n'
        '            obj["forza_emissive_mask_texture_texcoord"] = str(getattr(material_instance, "emissive_mask_texture_texcoord", "TEXCOORD0"))\n'
        '            obj["forza_emissive_mask_texture_tiling"] = f"{float(_emissive_mask_tiling[0])},{float(_emissive_mask_tiling[1])}"\n'
        '            if material_instance.diffuse_texture is not None:\n'
        '                obj["forza_diffuse_texture_path"] = material_instance.diffuse_texture.path\n'
        '            if material_instance.normal_texture is not None:\n'
        '                obj["forza_normal_texture_path"] = material_instance.normal_texture.path\n'
        '            if material_instance.gloss_texture is not None:\n'
        '                obj["forza_gloss_texture_path"] = material_instance.gloss_texture.path\n'
        '            if getattr(material_instance, "mask_texture", None) is not None:\n'
        '                obj["forza_mask_texture_path"] = material_instance.mask_texture.path\n'
        '            if material_instance.alpha_texture is not None:\n'
        '                obj["forza_alpha_texture_path"] = material_instance.alpha_texture.path\n'
        '                obj["forza_alpha_texture_output"] = int(material_instance.alpha_texture_output)\n'
        '            if getattr(material_instance, "emissive_texture", None) is not None:\n'
        '                obj["forza_emissive_texture_path"] = material_instance.emissive_texture.path\n'
        '            if getattr(material_instance, "emissive_mask_texture", None) is not None:\n'
        '                obj["forza_emissive_mask_texture_path"] = material_instance.emissive_mask_texture.path\n'
        '            _tint = getattr(material_instance, "diffuse_tint_color", None)\n'
        '            if _tint is not None:\n'
        '                obj["forza_diffuse_tint_color"] = [float(c) for c in _tint]\n',
    )

    # Patch: keep shader_name on MaterialInstance for downstream metadata export.
    script = script.replace(
        '        self.name = ""\n'
        '        self.valid = False\n',
        '        self.name = ""\n'
        '        self.shader_name = None\n'
        '        self.valid = False\n',
        1,
    )
    script = script.replace(
        '        self.alpha_texture = None\n'
        '        self.alpha_texture_output = 0 # channel\n',
        '        self.alpha_texture = None\n'
        '        self.alpha_texture_output = 0 # channel\n'
        '        self.alpha_texture_tiling = (1, 1)\n'
        '        self.alpha_texture_texcoord = "TEXCOORD0"\n'
        '        self.mask_texture = None\n'
        '        self.mask_texture_tiling = (1, 1)\n'
        '        self.mask_texture_texcoord = "TEXCOORD0"\n'
        '        self.emissive_texture = None\n'
        '        self.emissive_texture_tiling = (1, 1)\n'
        '        self.emissive_texture_texcoord = "TEXCOORD0"\n'
        '        self.emissive_mask_texture = None\n'
        '        self.emissive_mask_texture_tiling = (1, 1)\n'
        '        self.emissive_mask_texture_texcoord = "TEXCOORD0"\n'
        '        self.diffuse_tint_color = None\n',
        1,
    )
    script = script.replace(
        '        material = MaterialSystemObject()\n'
        '        material.deserialize(blob.stream)\n',
        '        material = MaterialSystemObject()\n'
        '        material.deserialize(blob.stream)\n'
        '        self.shader_name = material.shader_name\n',
        1,
    )
    script = script.replace(
        '        self.shader_name = material.shader_name\n'
        '\n'
        '        # universal:\n',
        '        self.shader_name = material.shader_name\n'
        '\n'
        '        standard_tiling = (1, 1)\n'
        '        standard_texcoord = "TEXCOORD0"\n'
        '        if ShaderParameterName.U_Tiling_floatVal in material.parameters and ShaderParameterName.V_Tiling_floatVal in material.parameters:\n'
        '            standard_tiling = (\n'
        '                material.parameters[ShaderParameterName.U_Tiling_floatVal].value,\n'
        '                material.parameters[ShaderParameterName.V_Tiling_floatVal].value,\n'
        '            )\n'
        '            if abs(standard_tiling[0] - 1) > 0.000001 or abs(standard_tiling[1] - 1) > 0.000001:\n'
        '                standard_texcoord = "TEXCOORD1"\n'
        '        if ShaderParameterName.BaseColorAlpha_Texture in material.parameters:\n'
        '            path = material.parameters[ShaderParameterName.BaseColorAlpha_Texture].path\n'
        '            if path != "":\n'
        '                self.diffuse_texture = Texture(path_resolver.resolve(path))\n'
        '                self.diffuse_texture_texcoord = standard_texcoord\n'
        '                self.diffuse_texture_tiling = standard_tiling\n'
        '        if ShaderParameterName.RoughMetalAO_Texture in material.parameters:\n'
        '            path = material.parameters[ShaderParameterName.RoughMetalAO_Texture].path\n'
        '            if path != "":\n'
        '                self.gloss_texture = Texture(path_resolver.resolve(path))\n'
        '                self.gloss_texture_texcoord = standard_texcoord\n'
        '                self.gloss_texture_tiling = standard_tiling\n'
        '        if ShaderParameterName.Standard_Normal_Texture in material.parameters:\n'
        '            path = material.parameters[ShaderParameterName.Standard_Normal_Texture].path\n'
        '            if path != "":\n'
        '                self.normal_texture = Texture(path_resolver.resolve(path))\n'
        '                self.normal_texture_texcoord = standard_texcoord\n'
        '                self.normal_texture_tiling = standard_tiling\n'
        '        if ShaderParameterName.Standard_Alpha_Texture in material.parameters:\n'
        '            path = material.parameters[ShaderParameterName.Standard_Alpha_Texture].path\n'
        '            if path != "":\n'
        '                self.alpha_texture = Texture(path_resolver.resolve(path))\n'
        '                self.alpha_texture_output = 0\n'
        '                self.alpha_texture_texcoord = standard_texcoord\n'
        '                self.alpha_texture_tiling = standard_tiling\n'
        '        if ShaderParameterName.Emissive_Lite_Texture in material.parameters:\n'
        '            path = material.parameters[ShaderParameterName.Emissive_Lite_Texture].path\n'
        '            if path != "":\n'
        '                self.emissive_texture = Texture(path_resolver.resolve(path))\n'
        '                self.emissive_texture_texcoord = standard_texcoord\n'
        '                self.emissive_texture_tiling = standard_tiling\n'
        '        if ShaderParameterName.Emissive_Mask_Texture in material.parameters:\n'
        '            path = material.parameters[ShaderParameterName.Emissive_Mask_Texture].path\n'
        '            if path != "":\n'
        '                self.emissive_mask_texture = Texture(path_resolver.resolve(path))\n'
        '                self.emissive_mask_texture_texcoord = standard_texcoord\n'
        '                self.emissive_mask_texture_tiling = standard_tiling\n'
        '        # Read per-instance tint color even when a diffuse texture is present.\n'
        '        # The universal fallback only reads color params when there is no\n'
        '        # texture, so we capture it here separately to preserve tint for\n'
        '        # tiling-textured materials like blkleather_mgl.\n'
        '        # First check the known named color param hashes from documented shaders.\n'
        '        for _tint_key in (\n'
        '            ShaderParameterName.DiffuseColorAColorParam,\n'
        '            ShaderParameterName.DiffuseColorGroupColorParam,\n'
        '            ShaderParameterName.DiffuseColorColorParam,\n'
        '            ShaderParameterName.ColorGroupColorParam,\n'
        '        ):\n'
        '            if _tint_key in material.parameters:\n'
        '                self.diffuse_tint_color = material.parameters[_tint_key].value\n'
        '                break\n'
        '        # Fallback: some FMNext shaders (e.g. leather) use material-specific\n'
        '        # color param hashes not listed in ShaderParameterName. Scan for any\n'
        '        # type=1 (Color_ShaderParameter) override and use it as the tint.\n'
        '        if self.diffuse_tint_color is None:\n'
        '            for _p in material.parameters.values():\n'
        '                if _p.type == 1:\n'
        '                    self.diffuse_tint_color = _p.value\n'
        '                    break\n'
        '\n'
        '        # universal:\n',
        1,
    )
    script = script.replace(
        '                case "m_ch2normglossalphaemissive":\n'
        '                    self.valid = True\n'
        '                    # self.diffuse_color = (1, 1, 1, 1)\n'
        '                    self.diffuse_color = (0, 0, 0, 1)\n'
        '                    self.gloss_value = 0 # placeholder, metal mirror\n',
        '                case "m_ch2normglossalphaemissive":\n'
        '                    self.valid = True\n'
        '                    # self.diffuse_color = (1, 1, 1, 1)\n'
        '                    self.diffuse_color = (0, 0, 0, 1)\n'
        '                    self.gloss_value = 0 # placeholder, metal mirror\n'
        '                case "car_carbonfiber" | "carbonfiber":\n'
        '                    tiling_u = material.parameters[ShaderParameterName.U_Tiling_floatVal].value if ShaderParameterName.U_Tiling_floatVal in material.parameters else 1\n'
        '                    tiling_v = material.parameters[ShaderParameterName.V_Tiling_floatVal].value if ShaderParameterName.V_Tiling_floatVal in material.parameters else tiling_u\n'
        '                    self.normal_texture_texcoord = "TEXCOORD1"\n'
        '                    self.normal_texture_tiling = (tiling_u, tiling_v)\n'
        '                    if ShaderParameterName.WeaveNormal_Texture in material.parameters:\n'
        '                        self.normal_texture = Texture(path_resolver.resolve(material.parameters[ShaderParameterName.WeaveNormal_Texture].path))\n'
        '                    self.mask_texture_texcoord = self.normal_texture_texcoord\n'
        '                    self.mask_texture_tiling = self.normal_texture_tiling\n'
        '                    if ShaderParameterName.WeaveMask_Texture in material.parameters:\n'
        '                        self.mask_texture = Texture(path_resolver.resolve(material.parameters[ShaderParameterName.WeaveMask_Texture].path))\n'
        '                    if ShaderParameterName.WeaveColorTintAColorParam in material.parameters:\n'
        '                        self.diffuse_color = material.parameters[ShaderParameterName.WeaveColorTintAColorParam].value\n',
        1,
    )

    # Body-only mode: skip non-exterior parts and interior model types
    if body_only:
        body_types_str = repr(BODY_PART_TYPES)
        excluded_str = repr(EXCLUDED_MODEL_TYPES)
        inject = (
            f'    if part.type not in {body_types_str}:\n'
            '        continue\n'
        )
        # Filter parts in both loops
        script = script.replace(
            'for part in [*scene.parts, *scene.upgradable_parts]:\n',
            'for part in [*scene.parts, *scene.upgradable_parts]:\n' + inject,
        )
        # Filter individual models by type name (skip interior/mechanical)
        script = script.replace(
            '        if model.draw_groups & requested_draw_group == 0:\n'
            '            continue\n',
            '        if model.draw_groups & requested_draw_group == 0:\n'
            '            continue\n'
            f'        if getattr(model, "type", "") in {excluded_str}:\n'
            '            continue\n'
            '        if hasattr(model, "bone_name") and any(model.bone_name.startswith(p) for p in ("controlArm", "spindle", "hub")):\n'
            '            continue\n'
            '        if "interior" in path_resolver.resolve(model.path).lower():\n'
            '            continue\n',
        )

    # Interior-only mode: keep only interior model types within CarBody
    if interior_only:
        interior_str = repr(INTERIOR_MODEL_TYPES)
        inject = (
            f'    if part.type not in {{2}}:\n'
            '        continue\n'
        )
        script = script.replace(
            'for part in [*scene.parts, *scene.upgradable_parts]:\n',
            'for part in [*scene.parts, *scene.upgradable_parts]:\n' + inject,
        )
        script = script.replace(
            '        if model.draw_groups & requested_draw_group == 0:\n'
            '            continue\n',
            '        if model.draw_groups & requested_draw_group == 0:\n'
            '            continue\n'
            f'        if getattr(model, "type", "") not in {interior_str} and "interior" not in path_resolver.resolve(model.path).lower():\n'
            '            continue\n',
        )

    return script


def _smart_material_script(texture_map: dict[str, Path] | None = None, manifest_path: str | None = None) -> str:
    """
    Returns a Blender Python snippet that assigns realistic Principled BSDF
    materials to every mesh object that has no material after the carbin import.
    Material type is inferred from keyword patterns in the object name, which
    mirrors the game's naming convention (e.g. 'carpaint', 'chrome', 'glass').

    When *texture_map* is provided, any material whose name matches a texture
    key will have an Image Texture node connected to the Base Color input of its
    Principled BSDF.  The lookup is case-insensitive substring matching against
    the object name.

    .. important::
        Texture keys must be lowercase base-names as returned by
        ``extract_textures()`` (e.g. ``"fer_badge_034"``).  Paths must be
        absolute and point to valid DDS files that Blender can load directly.
    """
    # Serialise the texture map for injection into the Blender script.
    # We emit it as a plain Python dict literal so no external files are needed.
    if texture_map:
        tex_items = ", ".join(
            f"{name!r}: r{str(path)!r}"
            for name, path in texture_map.items()
        )
        tex_map_literal = "{" + tex_items + "}"
    else:
        tex_map_literal = "{}"

    manifest_literal = repr(manifest_path.replace("\\", "/")) if manifest_path else "None"

    return f'''
# ── Smart material fallback ─────────────────────────────────────────────────
_TEXTURE_MAP = {tex_map_literal}
_MANIFEST_PATH = {manifest_literal}

def _make_material(name, base_color, metallic=0.0, roughness=0.5,
                   transmission=0.0, ior=1.45, emission=(0,0,0,1), emission_strength=0.0,
                   specular=0.5, sheen=0.0, coat_weight=0.0, coat_roughness=0.03):
    import bpy
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    if transmission > 0:
        mat.blend_method = "BLEND"
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    # Establish a consistent reference position for downstream texture nodes.
    bsdf.location = (300, 300)
    out_node = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out_node:
        out_node.location = (600, 300)
    bsdf.inputs["Base Color"].default_value = base_color
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["IOR"].default_value = ior
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = transmission
    elif "Transmission" in bsdf.inputs:
        bsdf.inputs["Transmission"].default_value = transmission
    if emission_strength > 0:
        bsdf.inputs["Emission Color"].default_value = emission
        bsdf.inputs["Emission Strength"].default_value = emission_strength
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = specular
    if "Sheen Weight" in bsdf.inputs:
        bsdf.inputs["Sheen Weight"].default_value = sheen
    if "Coat Weight" in bsdf.inputs:
        bsdf.inputs["Coat Weight"].default_value = coat_weight
        bsdf.inputs["Coat Roughness"].default_value = coat_roughness
    return mat


def _parse_tiling(value):
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
        if len(parts) < 2:
            return None
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return None
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError, IndexError):
        return None


def _wire_texture_vector(tree, tex_node, texcoord=None, tiling=None):
    texcoord = str(texcoord or "").strip()
    tiling = _parse_tiling(tiling)
    needs_texcoord = bool(texcoord and texcoord.upper() != "TEXCOORD0")
    needs_tiling = bool(tiling and (abs(tiling[0] - 1.0) > 0.000001 or abs(tiling[1] - 1.0) > 0.000001))
    if not needs_texcoord and not needs_tiling:
        return

    tx, ty = tex_node.location[0], tex_node.location[1]
    uv_node = tree.nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = texcoord or "TEXCOORD0"
    vector_socket = uv_node.outputs["UV"]

    if needs_tiling:
        scale_node = tree.nodes.new("ShaderNodeVectorMath")
        scale_node.operation = "MULTIPLY"
        scale_node.inputs[1].default_value = (tiling[0], tiling[1], 1.0)
        scale_node.location = (tx - 200, ty)
        uv_node.location = (tx - 400, ty)
        tree.links.new(vector_socket, scale_node.inputs[0])
        vector_socket = scale_node.outputs[0]
    else:
        uv_node.location = (tx - 250, ty)

    if "Vector" in tex_node.inputs:
        tree.links.new(vector_socket, tex_node.inputs["Vector"])


def _attach_texture(mat, tex_path, texcoord=None, tiling=None):
    """Wire an Image Texture node into *mat*\'s Principled BSDF Base Color input."""
    import bpy
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return
    # Load or reuse the image
    img = bpy.data.images.get(tex_path) or bpy.data.images.load(tex_path)
    img.colorspace_settings.name = "sRGB"
    # Create texture node to the upper-left of the BSDF (diffuse row)
    bx, by = bsdf.location[0], bsdf.location[1]
    tex_node = tree.nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    tex_node.location = (bx - 600, by + 300)
    _wire_texture_vector(tree, tex_node, texcoord=texcoord, tiling=tiling)
    tree.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])


def _attach_texture_slot(mat, tex_path, slot_name, non_color=True, invert=False,
                         texcoord=None, tiling=None, output_index=None):
    """Attach an image texture to a specific Principled slot."""
    import bpy
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None or slot_name not in bsdf.inputs:
        return

    img = bpy.data.images.get(tex_path) or bpy.data.images.load(tex_path)
    img.colorspace_settings.name = "Non-Color" if non_color else "sRGB"

    bx, by = bsdf.location[0], bsdf.location[1]
    # Position texture in the appropriate row based on destination slot.
    if slot_name == "Normal":
        tex_y = by - 250
    else:
        tex_y = by + 300
    tex_node = tree.nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    tex_node.location = (bx - 600, tex_y)
    _wire_texture_vector(tree, tex_node, texcoord=texcoord, tiling=tiling)

    out_socket = tex_node.outputs["Color"]
    if slot_name == "Normal":
        # Forza normals are DirectX convention (Y/green = down). Blender's
        # Normal Map node has no DIRECTX space enum, so flip G explicitly.
        try:
            separate = tree.nodes.new("ShaderNodeSeparateColor")
            if hasattr(separate, "mode"):
                separate.mode = "RGB"
            separate_input = separate.inputs["Color"]
            red_output = separate.outputs["Red"]
            green_output = separate.outputs["Green"]
            blue_output = separate.outputs["Blue"]
            combine = tree.nodes.new("ShaderNodeCombineColor")
            if hasattr(combine, "mode"):
                combine.mode = "RGB"
            red_input = combine.inputs["Red"]
            green_input = combine.inputs["Green"]
            blue_input = combine.inputs["Blue"]
            combine_output = combine.outputs["Color"]
        except RuntimeError:
            separate = tree.nodes.new("ShaderNodeSeparateRGB")
            separate_input = separate.inputs["Image"]
            red_output = separate.outputs["R"]
            green_output = separate.outputs["G"]
            blue_output = separate.outputs["B"]
            combine = tree.nodes.new("ShaderNodeCombineRGB")
            red_input = combine.inputs["R"]
            green_input = combine.inputs["G"]
            blue_input = combine.inputs["B"]
            combine_output = combine.outputs["Image"]

        separate.location = (bx - 450, tex_y)
        combine.location = (bx - 300, tex_y)
        flip_green = tree.nodes.new("ShaderNodeMath")
        flip_green.operation = "SUBTRACT"
        flip_green.inputs[0].default_value = 1.0
        flip_green.location = (bx - 450, tex_y - 160)
        tree.links.new(out_socket, separate_input)
        tree.links.new(red_output, red_input)
        tree.links.new(green_output, flip_green.inputs[1])
        tree.links.new(flip_green.outputs[0], green_input)
        tree.links.new(blue_output, blue_input)

        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        normal_map.space = "TANGENT"
        normal_map.location = (bx - 100, tex_y)
        if texcoord:
            normal_map.uv_map = str(texcoord)
        tree.links.new(combine_output, normal_map.inputs["Color"])
        out_socket = normal_map.outputs["Normal"]
    elif output_index is not None:
        try:
            out_socket = tex_node.outputs[int(output_index)]
        except (IndexError, TypeError, ValueError):
            pass
    elif invert:
        invert_node = tree.nodes.new("ShaderNodeInvert")
        tree.links.new(out_socket, invert_node.inputs["Color"])
        out_socket = invert_node.outputs["Color"]

    tree.links.new(out_socket, bsdf.inputs[slot_name])


def _attach_rmao_texture(mat, tex_path, diffuse_path=None, texcoord=None,
                         tiling=None, diffuse_texcoord=None, diffuse_tiling=None,
                         diffuse_tint=None):
    """Attach packed RMAO data: R=roughness, G=metallic, B=AO (AO not wired — path-traced renders)."""
    import bpy
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return

    bx, by = bsdf.location[0], bsdf.location[1]

    img = bpy.data.images.get(tex_path) or bpy.data.images.load(tex_path)
    img.colorspace_settings.name = "Non-Color"

    # RMAO texture goes in the middle row, Separate node beside it.
    tex_node = tree.nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    tex_node.location = (bx - 600, by)
    _wire_texture_vector(tree, tex_node, texcoord=texcoord, tiling=tiling)

    try:
        separate = tree.nodes.new("ShaderNodeSeparateColor")
        if hasattr(separate, "mode"):
            separate.mode = "RGB"
        separate_input = separate.inputs["Color"]
        red_output = separate.outputs["Red"]
        green_output = separate.outputs["Green"]
    except RuntimeError:
        separate = tree.nodes.new("ShaderNodeSeparateRGB")
        separate_input = separate.inputs["Image"]
        red_output = separate.outputs["R"]
        green_output = separate.outputs["G"]
    separate.location = (bx - 300, by)
    tree.links.new(tex_node.outputs["Color"], separate_input)

    if "Roughness" in bsdf.inputs:
        tree.links.new(red_output, bsdf.inputs["Roughness"])
    if "Metallic" in bsdf.inputs:
        tree.links.new(green_output, bsdf.inputs["Metallic"])

    # AO (blue channel) is intentionally not wired into Base Color.
    # Path-traced renders compute occlusion physically; baked AO would
    # double-darken geometry that is already self-occluded by the integrator.

    if diffuse_path:
        diffuse_img = bpy.data.images.get(diffuse_path) or bpy.data.images.load(diffuse_path)
        diffuse_img.colorspace_settings.name = "sRGB"
        diffuse_node = tree.nodes.new("ShaderNodeTexImage")
        diffuse_node.image = diffuse_img
        diffuse_node.location = (bx - 600, by + 300)
        _wire_texture_vector(tree, diffuse_node, texcoord=diffuse_texcoord, tiling=diffuse_tiling)
        diffuse_out = diffuse_node.outputs["Color"]

        if diffuse_tint is not None:
            # Multiply a per-material tint (e.g. black leather) into the texture.
            tint_node = tree.nodes.new("ShaderNodeRGB")
            tint_node.location = (bx - 600, by + 150)
            tint_node.outputs[0].default_value = diffuse_tint
            mix_tint = tree.nodes.new("ShaderNodeMixRGB")
            mix_tint.blend_type = "MULTIPLY"
            mix_tint.location = (bx - 300, by + 300)
            mix_tint.inputs["Fac" if "Fac" in mix_tint.inputs else "Factor"].default_value = 1.0
            tree.links.new(diffuse_out, mix_tint.inputs["Color1"])
            tree.links.new(tint_node.outputs["Color"], mix_tint.inputs["Color2"])
            diffuse_out = mix_tint.outputs["Color"]

        tree.links.new(diffuse_out, bsdf.inputs["Base Color"])
    else:
        c = diffuse_tint or bsdf.inputs["Base Color"].default_value
        bsdf.inputs["Base Color"].default_value = c


def _attach_carbon_weave_mask(mat, tex_path, texcoord=None, tiling=None, tint=None):
    """Use the car_carbonfiber weave mask as visible dark carbon variation."""
    import bpy
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return

    bx, by = bsdf.location[0], bsdf.location[1]
    img = bpy.data.images.get(tex_path) or bpy.data.images.load(tex_path)
    img.colorspace_settings.name = "Non-Color"

    tex_node = tree.nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    tex_node.location = (bx - 600, by + 300)
    _wire_texture_vector(tree, tex_node, texcoord=texcoord, tiling=tiling)

    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.location = (bx - 300, by + 300)
    ramp.color_ramp.elements[0].position = 0.18
    ramp.color_ramp.elements[0].color = (0.012, 0.012, 0.012, 1.0)
    ramp.color_ramp.elements[1].position = 1.0
    ramp.color_ramp.elements[1].color = (0.11, 0.11, 0.10, 1.0)
    tree.links.new(tex_node.outputs["Color"], ramp.inputs["Fac"])

    color_socket = ramp.outputs["Color"]
    if tint is not None:
        tint_node = tree.nodes.new("ShaderNodeRGB")
        tint_node.location = (bx - 600, by + 120)
        tint_node.outputs[0].default_value = tint
        mix_tint = tree.nodes.new("ShaderNodeMixRGB")
        mix_tint.blend_type = "MULTIPLY"
        mix_tint.location = (bx - 100, by + 300)
        mix_tint.inputs["Fac" if "Fac" in mix_tint.inputs else "Factor"].default_value = 1.0
        tree.links.new(color_socket, mix_tint.inputs["Color1"])
        tree.links.new(tint_node.outputs["Color"], mix_tint.inputs["Color2"])
        color_socket = mix_tint.outputs["Color"]

    tree.links.new(color_socket, bsdf.inputs["Base Color"])


def _attach_emission_texture(mat, tex_path, texcoord=None, tiling=None,
                             mask_path=None, mask_texcoord=None, mask_tiling=None,
                             strength=1.5):
    import bpy
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None or "Emission Color" not in bsdf.inputs:
        return

    bx, by = bsdf.location[0], bsdf.location[1]
    img = bpy.data.images.get(tex_path) or bpy.data.images.load(tex_path)
    img.colorspace_settings.name = "sRGB"

    tex_node = tree.nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    tex_node.location = (bx - 600, by + 600)
    _wire_texture_vector(tree, tex_node, texcoord=texcoord, tiling=tiling)
    emission_socket = tex_node.outputs["Color"]

    if mask_path:
        mask_img = bpy.data.images.get(mask_path) or bpy.data.images.load(mask_path)
        mask_img.colorspace_settings.name = "Non-Color"
        mask_node = tree.nodes.new("ShaderNodeTexImage")
        mask_node.image = mask_img
        mask_node.location = (bx - 600, by + 450)
        _wire_texture_vector(tree, mask_node, texcoord=mask_texcoord, tiling=mask_tiling)

        mix_mask = tree.nodes.new("ShaderNodeMixRGB")
        mix_mask.blend_type = "MULTIPLY"
        mix_mask.location = (bx - 300, by + 600)
        mix_mask.inputs["Fac" if "Fac" in mix_mask.inputs else "Factor"].default_value = 1.0
        tree.links.new(emission_socket, mix_mask.inputs["Color1"])
        tree.links.new(mask_node.outputs["Color"], mix_mask.inputs["Color2"])
        emission_socket = mix_mask.outputs["Color"]

    tree.links.new(emission_socket, bsdf.inputs["Emission Color"])
    if "Emission Strength" in bsdf.inputs:
        bsdf.inputs["Emission Strength"].default_value = strength


def _extract_swatch_parts_from_path(swatch_path):
    """Return (base_name, channel) parsed from a swatch path."""
    import re
    if not swatch_path:
        return (None, None)
    filename = str(swatch_path).replace("\\\\", "/").rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0].lower()
    marker_match = re.search(r"_(basecoloralpha|nrml|normal|glos|rmao|ao|alpha|mask\\d*|lite|diffuse)_", stem)
    if marker_match:
        channel = marker_match.group(1)
        base_name = stem.split("_" + channel + "_", 1)[0]
        return (base_name, channel)
    return (stem, None)


def _resolve_texture_from_swatch_path(swatch_path):
    """Resolve swatch path to extracted image path in _TEXTURE_MAP."""
    base_name, channel = _extract_swatch_parts_from_path(swatch_path)
    if not base_name:
        return (None, None)

    candidate_keys = [base_name]
    if channel and channel != "basecoloralpha":
        candidate_keys.insert(0, f"{{base_name}}__{{channel}}")

    for key in candidate_keys:
        if key in _TEXTURE_MAP:
            return (str(_TEXTURE_MAP[key]).replace("\\\\", "/"), key)
    return (None, None)


def _find_texture_for_material_name(material_name):
    """Return (path, key) using token-aware matching on a material name."""
    if not material_name:
        return (None, None)

    # Token-boundary search to avoid accidental hits like "r_001" matching
    # "carbonfiber_001" (substring-only matching is too permissive).
    search_in = "_" + material_name.lower().strip("_") + "_"
    best_key = None
    for key in _TEXTURE_MAP:
        if "__" in key:
            # Skip channel-specific keys when searching for diffuse fallback.
            continue
        token = "_" + key.lower().strip("_") + "_"
        if token in search_in:
            if best_key is None or len(key) > len(best_key):
                best_key = key

    if best_key is None:
        return (None, None)

    return (str(_TEXTURE_MAP[best_key]).replace("\\\\", "/"), best_key)


def _find_texture(obj):
    """Return (path, key) preferring importer metadata over string heuristics."""
    # Data-driven path: use exact diffuse swatch path resolved by carbin_importer.
    swatch_path = obj.get("forza_diffuse_texture_path")
    resolved_path, resolved_key = _resolve_texture_from_swatch_path(swatch_path)
    if resolved_path:
        return (resolved_path, resolved_key)

    # Fallback: material name from importer metadata, then object suffix.
    material_name = obj.get("forza_material_name")
    if not material_name:
        parts = obj.name.split(" ", 1)
        material_name = parts[1] if len(parts) > 1 else parts[0]

    return _find_texture_for_material_name(material_name)


def _resolve_material(obj_name):
    n = obj_name.lower()
    # Glass / transparent
    if any(k in n for k in ("glass", "windshield", "window", "visor", "lens")):
        return _make_material("_auto_glass",
            base_color=(0.8, 0.9, 1.0, 1.0), metallic=0.0, roughness=0.05,
            transmission=0.92, ior=1.52, specular=0.5)
    if any(k in n for k in ("headlightbulb", "taillightbulb", "bulb")):
        return _make_material("_auto_bulb",
            base_color=(1.0, 0.97, 0.85, 1.0), metallic=0.0, roughness=0.02,
            transmission=0.85, ior=1.5, emission=(1.0,0.95,0.7,1.0), emission_strength=2.0)
    # Carbon fibre (check before mirror/chrome to prevent misclassification)
    if any(k in n for k in ("carbon", "cfrp", "carbonfiber")):
        return _make_material("_auto_carbon",
            base_color=(0.03, 0.03, 0.03, 1.0), metallic=0.0, roughness=0.3,
            coat_weight=0.8, coat_roughness=0.05, specular=0.4)
    # Chrome / mirror
    if any(k in n for k in ("chrome", "mirror", "polished")):
        return _make_material("_auto_chrome",
            base_color=(0.95, 0.95, 0.95, 1.0), metallic=1.0, roughness=0.0, specular=1.0)
    # Car paint
    if "carpaint" in n or "car_paint" in n:
        return _make_material("_auto_carpaint",
            base_color=(0.55, 0.04, 0.04, 1.0), metallic=0.1, roughness=0.25,
            coat_weight=1.0, coat_roughness=0.03, specular=0.6)
    # Rubber / tyre
    if any(k in n for k in ("rubber", "tire", "tyre", "gasket", "seal")):
        return _make_material("_auto_rubber",
            base_color=(0.03, 0.03, 0.03, 1.0), metallic=0.0, roughness=0.95)
    # Brake rotor / bare steel
    if any(k in n for k in ("rotor", "brake", "brk")):
        return _make_material("_auto_brake_rotor",
            base_color=(0.3, 0.28, 0.26, 1.0), metallic=0.85, roughness=0.55)
    # Painted metal (body panels, not carpaint)
    if any(k in n for k in ("metalpainted", "metal_painted", "painted_metal")):
        return _make_material("_auto_metal_painted",
            base_color=(0.18, 0.18, 0.18, 1.0), metallic=0.6, roughness=0.35)
    # Bare / brushed metal
    if any(k in n for k in ("metal", "steel", "alumin", "exhaust", "pipe")):
        return _make_material("_auto_metal",
            base_color=(0.65, 0.65, 0.65, 1.0), metallic=0.95, roughness=0.3)
    # Black plastic / trim
    if any(k in n for k in ("blkplastic", "blackplastic", "plastic", "trim", "grille", "vent")):
        return _make_material("_auto_plastic_black",
            base_color=(0.04, 0.04, 0.04, 1.0), metallic=0.0, roughness=0.55)
    # Interior leather / upholstery
    if any(k in n for k in ("leather", "seat", "seats", "upholstery", "dash", "console")):
        return _make_material("_auto_leather",
            base_color=(0.06, 0.04, 0.03, 1.0), metallic=0.0, roughness=0.65,
            sheen=0.3)
    # Interior / general
    if any(k in n for k in ("interior", "cockpit", "gauges", "floor", "pillar")):
        return _make_material("_auto_interior_dark",
            base_color=(0.05, 0.05, 0.05, 1.0), metallic=0.0, roughness=0.7)
    # Chassis / undercarriage
    if any(k in n for k in ("chassis", "undercarriage", "underbody", "frame")):
        return _make_material("_auto_chassis",
            base_color=(0.06, 0.06, 0.06, 1.0), metallic=0.3, roughness=0.8)
    # Caliper
    if "caliper" in n:
        return _make_material("_auto_caliper",
            base_color=(0.7, 0.07, 0.07, 1.0), metallic=0.0, roughness=0.4)
    # Wheel / rim
    if any(k in n for k in ("wheel", "rim", "hub", "spindle")):
        return _make_material("_auto_wheel",
            base_color=(0.2, 0.2, 0.2, 1.0), metallic=0.9, roughness=0.2)
    # Generic fallback
    return _make_material("_auto_default",
        base_color=(0.5, 0.5, 0.5, 1.0), metallic=0.0, roughness=0.5)


def _manifest_classification_name(obj, entry):
    material_name = str(entry.get("forza_material_name") or "")
    # Forza often stores the owning mesh after a pipe, e.g.
    # "seat_belt_002|visors_a".  The suffix is not the material identity and
    # can badly skew keyword classification.
    primary_material = material_name.split("|", 1)[0].strip()
    shader_name = str(entry.get("forza_shader_name") or "").lower()
    if shader_name == "car_window":
        return f"glass {{primary_material}} {{obj.name}}"
    return primary_material or obj.name


def _new_material_from_template(template, name):
    # _resolve_material returns shared fallback singletons such as _auto_glass.
    # Manifest materials must be isolated before renaming or adding texture nodes.
    mat = template.copy()
    mat.name = name
    return mat


def _collect_material_manifest(objects):
    """Build data-driven mesh->material->texture manifest from importer metadata."""
    entries = []
    for obj in objects:
        if obj.type != "MESH":
            continue

        entry = {{
            "object_name": obj.name,
            "forza_material_id": obj.get("forza_material_id"),
            "forza_material_name": obj.get("forza_material_name"),
            "forza_shader_name": obj.get("forza_shader_name"),
            "diffuse_swatch_path": obj.get("forza_diffuse_texture_path"),
            "diffuse_texture_texcoord": obj.get("forza_diffuse_texture_texcoord"),
            "diffuse_texture_tiling": obj.get("forza_diffuse_texture_tiling"),
            "normal_swatch_path": obj.get("forza_normal_texture_path"),
            "normal_texture_texcoord": obj.get("forza_normal_texture_texcoord"),
            "normal_texture_tiling": obj.get("forza_normal_texture_tiling"),
            "gloss_swatch_path": obj.get("forza_gloss_texture_path"),
            "gloss_texture_texcoord": obj.get("forza_gloss_texture_texcoord"),
            "gloss_texture_tiling": obj.get("forza_gloss_texture_tiling"),
            "rmao_swatch_path": obj.get("forza_rmao_texture_path"),
            "mask_swatch_path": obj.get("forza_mask_texture_path"),
            "mask_texture_texcoord": obj.get("forza_mask_texture_texcoord"),
            "mask_texture_tiling": obj.get("forza_mask_texture_tiling"),
            "alpha_swatch_path": obj.get("forza_alpha_texture_path"),
            "alpha_texture_texcoord": obj.get("forza_alpha_texture_texcoord"),
            "alpha_texture_tiling": obj.get("forza_alpha_texture_tiling"),
            "alpha_texture_output": obj.get("forza_alpha_texture_output"),
            "emissive_swatch_path": obj.get("forza_emissive_texture_path"),
            "emissive_texture_texcoord": obj.get("forza_emissive_texture_texcoord"),
            "emissive_texture_tiling": obj.get("forza_emissive_texture_tiling"),
            "emissive_mask_swatch_path": obj.get("forza_emissive_mask_texture_path"),
            "emissive_mask_texture_texcoord": obj.get("forza_emissive_mask_texture_texcoord"),
            "emissive_mask_texture_tiling": obj.get("forza_emissive_mask_texture_tiling"),
            "diffuse_tint_color": list(obj.get("forza_diffuse_tint_color")) if obj.get("forza_diffuse_tint_color") is not None else None,
        }}

        has_manifest_data = any(
            entry[k] is not None
            for k in (
                "forza_material_id",
                "forza_material_name",
                "forza_shader_name",
                "diffuse_swatch_path",
                "diffuse_texture_texcoord",
                "diffuse_texture_tiling",
                "normal_swatch_path",
                "normal_texture_texcoord",
                "normal_texture_tiling",
                "gloss_swatch_path",
                "gloss_texture_texcoord",
                "gloss_texture_tiling",
                "rmao_swatch_path",
                "mask_swatch_path",
                "mask_texture_texcoord",
                "mask_texture_tiling",
                "alpha_swatch_path",
                "alpha_texture_texcoord",
                "alpha_texture_tiling",
                "alpha_texture_output",
                "emissive_swatch_path",
                "emissive_texture_texcoord",
                "emissive_texture_tiling",
                "emissive_mask_swatch_path",
                "emissive_mask_texture_texcoord",
                "emissive_mask_texture_tiling",
            )
        )
        if not has_manifest_data:
            continue

        _resolve_entry_texture_slots(entry)
        entries.append(entry)

    return entries


def _resolve_entry_texture_slots(entry):
    """Populate resolved_* fields on a manifest entry from data-driven sources."""
    entry["resolved_diffuse_path"], entry["resolved_diffuse_key"] = _resolve_texture_from_swatch_path(entry.get("diffuse_swatch_path"))
    entry["resolved_normal_path"], entry["resolved_normal_key"] = _resolve_texture_from_swatch_path(entry.get("normal_swatch_path"))
    gloss_path, gloss_key = _resolve_texture_from_swatch_path(entry.get("gloss_swatch_path"))
    entry["resolved_gloss_path"] = gloss_path
    entry["resolved_gloss_key"] = gloss_key
    entry["resolved_rmao_path"] = None
    entry["resolved_rmao_key"] = None
    entry["resolved_mask_path"], entry["resolved_mask_key"] = _resolve_texture_from_swatch_path(entry.get("mask_swatch_path"))
    if entry.get("gloss_swatch_path"):
        _, gloss_channel = _extract_swatch_parts_from_path(entry.get("gloss_swatch_path"))
        if gloss_channel == "rmao":
            entry["resolved_rmao_path"] = gloss_path
            entry["resolved_rmao_key"] = gloss_key
    entry["resolved_alpha_path"], entry["resolved_alpha_key"] = _resolve_texture_from_swatch_path(entry.get("alpha_swatch_path"))
    entry["resolved_emissive_path"], entry["resolved_emissive_key"] = _resolve_texture_from_swatch_path(entry.get("emissive_swatch_path"))
    entry["resolved_emissive_mask_path"], entry["resolved_emissive_mask_key"] = _resolve_texture_from_swatch_path(entry.get("emissive_mask_swatch_path"))

    # Fallback by exact material-name channel keys when explicit slot swatch
    # paths are unavailable from importer metadata.
    material_key = (entry.get("forza_material_name") or "").lower().strip()
    if material_key:
        if entry["resolved_diffuse_path"] is None and material_key in _TEXTURE_MAP:
            entry["resolved_diffuse_key"] = material_key
            entry["resolved_diffuse_path"] = str(_TEXTURE_MAP[material_key]).replace("\\\\", "/")

        if entry["resolved_normal_path"] is None:
            normal_key = next((k for k in (f"{{material_key}}__nrml", f"{{material_key}}__normal") if k in _TEXTURE_MAP), None)
            if normal_key:
                entry["resolved_normal_key"] = normal_key
                entry["resolved_normal_path"] = str(_TEXTURE_MAP[normal_key]).replace("\\\\", "/")

        if entry["resolved_gloss_path"] is None:
            gloss_key = next((k for k in (f"{{material_key}}__glos", f"{{material_key}}__rmao") if k in _TEXTURE_MAP), None)
            if gloss_key:
                entry["resolved_gloss_key"] = gloss_key
                entry["resolved_gloss_path"] = str(_TEXTURE_MAP[gloss_key]).replace("\\\\", "/")

        if entry["resolved_rmao_path"] is None:
            rmao_key = next((k for k in (f"{{material_key}}__rmao",) if k in _TEXTURE_MAP), None)
            if rmao_key:
                entry["resolved_rmao_key"] = rmao_key
                entry["resolved_rmao_path"] = str(_TEXTURE_MAP[rmao_key]).replace("\\\\", "/")

        # Do not treat *_mask as opacity. Shaders such as car_carbonfiber use
        # masks as WeaveMask_Texture, separate from Alpha_Texture.
        mask_key = next((k for k in (f"{{material_key}}__mask",) if k in _TEXTURE_MAP), None)
        if mask_key and entry["resolved_mask_path"] is None:
            entry["resolved_mask_key"] = mask_key
            entry["resolved_mask_path"] = str(_TEXTURE_MAP[mask_key]).replace("\\\\", "/")

        if entry["resolved_alpha_path"] is None:
            alpha_key = next((k for k in (f"{{material_key}}__alpha",) if k in _TEXTURE_MAP), None)
            if alpha_key:
                entry["resolved_alpha_key"] = alpha_key
                entry["resolved_alpha_path"] = str(_TEXTURE_MAP[alpha_key]).replace("\\\\", "/")

        if entry["resolved_emissive_path"] is None:
            emissive_key = next((k for k in (f"{{material_key}}__lite", f"{{material_key}}__diffuse") if k in _TEXTURE_MAP), None)
            if emissive_key:
                entry["resolved_emissive_key"] = emissive_key
                entry["resolved_emissive_path"] = str(_TEXTURE_MAP[emissive_key]).replace("\\\\", "/")

        if entry["resolved_emissive_mask_path"] is None:
            emissive_mask_key = next((k for k in (f"{{material_key}}__mask", f"{{material_key}}__mask2") if k in _TEXTURE_MAP), None)
            if emissive_mask_key:
                entry["resolved_emissive_mask_key"] = emissive_mask_key
                entry["resolved_emissive_mask_path"] = str(_TEXTURE_MAP[emissive_mask_key]).replace("\\\\", "/")

    shader_key = str(entry.get("forza_shader_name") or "").lower().strip()
    if shader_key in ("car_carbonfiber", "carbonfiber"):
        carbon_name = (entry.get("forza_material_name") or "").lower().split("|", 1)[0]
        if "forged" in carbon_name or "froged" in carbon_name or "custom" in carbon_name:
            carbon_bases = ("forged_carbon", "froged_carbonfiber", "carbonfiber_003", "carbonfiber_001")
        elif "kevlar" in carbon_name:
            carbon_bases = ("carbonkevlarweave", "carbonkevlarweave_002", "carbonfiber_001")
        else:
            carbon_bases = ("carbonfiber_001", "carbonfiber_003", "carbonfiber_002", "carbonfiber_007", "carbonfiber_016", "carbonfiber_027")

        if entry["resolved_normal_path"] is None:
            normal_key = next(
                (
                    key
                    for base in carbon_bases
                    for key in (f"{{base}}__normal", f"{{base}}__nrml")
                    if key in _TEXTURE_MAP
                ),
                None,
            )
            if normal_key:
                entry["resolved_normal_key"] = normal_key
                entry["resolved_normal_path"] = str(_TEXTURE_MAP[normal_key]).replace("\\\\", "/")

        if entry["resolved_mask_path"] is None:
            mask_key = next(
                (
                    key
                    for base in carbon_bases
                    for key in (f"{{base}}__mask", f"{{base}}__rmao", f"{{base}}__glos")
                    if key in _TEXTURE_MAP
                ),
                None,
            )
            if mask_key:
                entry["resolved_mask_key"] = mask_key
                entry["resolved_mask_path"] = str(_TEXTURE_MAP[mask_key]).replace("\\\\", "/")

    return entry


def _write_material_manifest(entries):
    if not _MANIFEST_PATH:
        return
    import json
    import os

    payload = {{
        "version": 2,
        "object_count": len(entries),
        "entries": entries,
    }}
    os.makedirs(os.path.dirname(_MANIFEST_PATH), exist_ok=True)
    with open(_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[MATERIAL MANIFEST] Saved {{_MANIFEST_PATH}} ({{len(entries)}} entries)")


def _manifest_material_digest(entry):
    import hashlib

    fields = (
        "forza_material_name",
        "forza_shader_name",
        "resolved_diffuse_key",
        "diffuse_texture_texcoord",
        "diffuse_texture_tiling",
        "resolved_normal_key",
        "normal_texture_texcoord",
        "normal_texture_tiling",
        "resolved_gloss_key",
        "gloss_texture_texcoord",
        "gloss_texture_tiling",
        "resolved_rmao_key",
        "resolved_mask_key",
        "mask_texture_texcoord",
        "mask_texture_tiling",
        "resolved_alpha_key",
        "alpha_texture_texcoord",
        "alpha_texture_tiling",
        "alpha_texture_output",
        "resolved_emissive_key",
        "emissive_texture_texcoord",
        "emissive_texture_tiling",
        "resolved_emissive_mask_key",
        "emissive_mask_texture_texcoord",
        "emissive_mask_texture_tiling",
        "diffuse_tint_color",
    )
    text = "|".join(str(entry.get(field) or "") for field in fields)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _apply_manifest_material(obj, entry):
    """Assign material from manifest slots; no name guessing for covered objects."""
    entry = _resolve_entry_texture_slots(entry)
    mat_tag = (entry.get("forza_material_name") or obj.name).replace(" ", "_").lower()
    base_mat_name = "_forza_manifest_" + mat_tag
    material_digest = _manifest_material_digest(entry)
    mat_name = base_mat_name

    import bpy
    mat = bpy.data.materials.get(mat_name)
    if mat is not None and mat.get("forza_material_digest") != material_digest:
        mat_name = base_mat_name + "__" + material_digest
        mat = bpy.data.materials.get(mat_name)
    if mat is None:
        source_name = _manifest_classification_name(obj, entry)
        mat = _new_material_from_template(_resolve_material(source_name), mat_name)
        mat["forza_material_digest"] = material_digest
        mat["forza_material_name"] = entry.get("forza_material_name") or ""
        mat["forza_shader_name"] = entry.get("forza_shader_name") or ""

        diffuse_path = entry.get("resolved_diffuse_path")
        normal_path = entry.get("resolved_normal_path")
        gloss_path = entry.get("resolved_gloss_path")
        rmao_path = entry.get("resolved_rmao_path")
        mask_path = entry.get("resolved_mask_path")
        alpha_path = entry.get("resolved_alpha_path")
        emissive_path = entry.get("resolved_emissive_path")
        emissive_mask_path = entry.get("resolved_emissive_mask_path")
        diffuse_texcoord = entry.get("diffuse_texture_texcoord")
        normal_texcoord = entry.get("normal_texture_texcoord")
        gloss_texcoord = entry.get("gloss_texture_texcoord")
        mask_texcoord = entry.get("mask_texture_texcoord")
        alpha_texcoord = entry.get("alpha_texture_texcoord")
        emissive_texcoord = entry.get("emissive_texture_texcoord")
        emissive_mask_texcoord = entry.get("emissive_mask_texture_texcoord")
        diffuse_tiling = entry.get("diffuse_texture_tiling")
        normal_tiling = entry.get("normal_texture_tiling")
        gloss_tiling = entry.get("gloss_texture_tiling")
        mask_tiling = entry.get("mask_texture_tiling")
        alpha_tiling = entry.get("alpha_texture_tiling")
        emissive_tiling = entry.get("emissive_texture_tiling")
        emissive_mask_tiling = entry.get("emissive_mask_texture_tiling")
        raw_tint = entry.get("diffuse_tint_color")
        # Apply tint only when it is meaningfully non-white; pure black (0,0,0,1)
        # with a texture means the game intends to tint the texture to that colour
        # (e.g. blkleather_mgl multiplies a near-black tint into the leather grain).
        diffuse_tint = tuple(raw_tint) if (raw_tint is not None and not all(abs(c - 1.0) < 0.001 for c in raw_tint[:3])) else None

        if rmao_path:
            _attach_rmao_texture(
                mat,
                rmao_path,
                diffuse_path,
                texcoord=gloss_texcoord,
                tiling=gloss_tiling,
                diffuse_texcoord=diffuse_texcoord,
                diffuse_tiling=diffuse_tiling,
                diffuse_tint=diffuse_tint,
            )
        elif diffuse_path:
            if diffuse_tint is not None:
                # No RMAO, but there is a tint to apply.
                import bpy as _bpy_tint
                tree = mat.node_tree
                bsdf = tree.nodes.get("Principled BSDF")
                bx, by = bsdf.location[0], bsdf.location[1]
                diffuse_img = _bpy_tint.data.images.get(diffuse_path) or _bpy_tint.data.images.load(diffuse_path)
                diffuse_img.colorspace_settings.name = "sRGB"
                diffuse_node = tree.nodes.new("ShaderNodeTexImage")
                diffuse_node.image = diffuse_img
                diffuse_node.location = (bx - 600, by + 300)
                _wire_texture_vector(tree, diffuse_node, texcoord=diffuse_texcoord, tiling=diffuse_tiling)
                tint_node = tree.nodes.new("ShaderNodeRGB")
                tint_node.location = (bx - 600, by + 150)
                tint_node.outputs[0].default_value = diffuse_tint
                mix_tint = tree.nodes.new("ShaderNodeMixRGB")
                mix_tint.blend_type = "MULTIPLY"
                mix_tint.location = (bx - 300, by + 300)
                mix_tint.inputs["Fac" if "Fac" in mix_tint.inputs else "Factor"].default_value = 1.0
                tree.links.new(diffuse_node.outputs["Color"], mix_tint.inputs["Color1"])
                tree.links.new(tint_node.outputs["Color"], mix_tint.inputs["Color2"])
                tree.links.new(mix_tint.outputs["Color"], bsdf.inputs["Base Color"])
            else:
                _attach_texture(mat, diffuse_path, texcoord=diffuse_texcoord, tiling=diffuse_tiling)
        elif str(entry.get("forza_shader_name") or "").lower() in ("car_carbonfiber", "carbonfiber") and mask_path:
            _attach_carbon_weave_mask(
                mat,
                mask_path,
                texcoord=mask_texcoord,
                tiling=mask_tiling,
                tint=diffuse_tint,
            )
        if normal_path:
            _attach_texture_slot(
                mat,
                normal_path,
                "Normal",
                non_color=True,
                texcoord=normal_texcoord,
                tiling=normal_tiling,
            )
        if gloss_path and not rmao_path:
            _attach_texture_slot(
                mat,
                gloss_path,
                "Roughness",
                non_color=True,
                texcoord=gloss_texcoord,
                tiling=gloss_tiling,
            )
        if alpha_path:
            _attach_texture_slot(
                mat,
                alpha_path,
                "Alpha",
                non_color=True,
                texcoord=alpha_texcoord,
                tiling=alpha_tiling,
                output_index=entry.get("alpha_texture_output"),
            )
            mat.blend_method = "BLEND"
        elif entry.get("forza_shader_name") == "car_label" and diffuse_path:
            # car_label bakes the cutout mask into the A channel of its
            # BaseColorAlpha diffuse texture. Standard_Alpha_Texture in the
            # parent materialbin (label_ch1.materialbin) is flat_texture_white,
            # which carries no channel marker in its filename so it is never
            # extracted into _TEXTURE_MAP and resolved_alpha_path is always null.
            # The fix: wire the Alpha output of the already-loaded diffuse
            # texture node directly to the BSDF Alpha input.
            _attach_texture_slot(
                mat,
                diffuse_path,
                "Alpha",
                non_color=False,
                texcoord=diffuse_texcoord,
                tiling=diffuse_tiling,
                output_index=1,
            )
            mat.blend_method = "BLEND"
        if emissive_path:
            _attach_emission_texture(
                mat,
                emissive_path,
                texcoord=emissive_texcoord,
                tiling=emissive_tiling,
                mask_path=emissive_mask_path,
                mask_texcoord=emissive_mask_texcoord,
                mask_tiling=emissive_mask_tiling,
            )

    if len(obj.data.materials) == 0:
        obj.data.materials.append(mat)
    else:
        obj.data.materials[0] = mat
    return True

import bpy as _bpy
_manifest_entries = _collect_material_manifest(_bpy.data.objects)
_manifest_by_object = {{entry["object_name"]: entry for entry in _manifest_entries}}
_write_material_manifest(_manifest_entries)

for _obj in _bpy.data.objects:
    if _obj.type != "MESH":
        continue
    _manifest_entry = _manifest_by_object.get(_obj.name)
    if _manifest_entry:
        _resolve_entry_texture_slots(_manifest_entry)
        if any(_manifest_entry.get(k) for k in ("resolved_diffuse_path", "resolved_normal_path", "resolved_gloss_path", "resolved_rmao_path", "resolved_mask_path", "resolved_alpha_path", "resolved_emissive_path", "resolved_emissive_mask_path")):
            _apply_manifest_material(_obj, _manifest_entry)
            continue

    _needs_material = len(_obj.data.materials) == 0 or _obj.data.materials[0] is None
    if _needs_material:

        # If this object has a matching texture, assign a per-texture material so we
        # do not contaminate the shared _auto_default with an unrelated image texture.
        _tex_path, _tex_key = _find_texture(_obj) if _TEXTURE_MAP else (None, None)
        if _tex_path:
            _mat_name = f"_tex_{{_tex_key}}" if _tex_key else "_auto_default"
            _mat = _bpy.data.materials.get(_mat_name)
            if _mat is None:
                # Bootstrap from the fallback material properties then rename
                _mat = _new_material_from_template(_resolve_material(_obj.name), _mat_name)
                _attach_texture(_mat, _tex_path)
        else:
            _mat = _resolve_material(_obj.name)
        if len(_obj.data.materials) == 0:
            _obj.data.materials.append(_mat)
        else:
            _obj.data.materials[0] = _mat
# ── End smart material fallback ─────────────────────────────────────────────
'''


def _prepare_output_path(output_path: Path) -> float | None:
    """Remove a stale export so success cannot be inferred from an old file."""
    if not output_path.exists():
        return None

    previous_mtime = output_path.stat().st_mtime
    output_path.unlink()
    return previous_mtime


def _is_fresh_output(output_path: Path, previous_mtime: float | None) -> bool:
    """Return True only when this run created or rewrote the export file."""
    if not output_path.exists():
        return False

    if previous_mtime is None:
        return True

    return output_path.stat().st_mtime > previous_mtime


def _important_process_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Keep only the Blender log markers that confirm file lifecycle events."""
    markers = ("[OVERWRITE]", "[SAVED]", "[EXPORTED OBJ]")
    return [line.strip() for line in result.stdout.splitlines() if any(marker in line for marker in markers)]


def _failure_process_lines(result: subprocess.CompletedProcess[str], limit: int = 8) -> list[str]:
    """Return the most useful diagnostic lines from a failed Blender run."""
    lines = [line.strip() for line in (result.stderr.splitlines() + result.stdout.splitlines()) if line.strip()]
    traceback_index = next((i for i, line in enumerate(lines) if line.startswith("Traceback ")), None)
    if traceback_index is not None:
        traceback_lines = []
        for line in lines[traceback_index:]:
            if line.startswith("Exception ignored in:") and traceback_lines:
                break
            traceback_lines.append(line)
            if len(traceback_lines) >= max(limit, 16):
                break
        return traceback_lines

    error_lines = [
        line
        for line in lines
        if "error" in line.lower() or "runtimeerror" in line.lower() or "traceback" in line.lower()
    ]
    if error_lines:
        return error_lines[-limit:]
    return lines[-limit:]


def run_blender_import(game_path: str, media_name: str, output_blend: Path, body_only: bool = False, interior_only: bool = False, texture_map: dict[str, Path] | None = None) -> subprocess.CompletedProcess:
    script_content = generate_blender_script(game_path, media_name, body_only=body_only, interior_only=interior_only)
    save_path = str(output_blend).replace("\\", "/")
    manifest_path = str(output_blend.with_suffix(".materials.json")).replace("\\", "/")

    # Prepend: clear default scene objects (cube, camera, light)
    preamble = (
        'import bpy\n'
        'bpy.ops.object.select_all(action="SELECT")\n'
        'bpy.ops.object.delete()\n'
    )
    script_content = script_content.replace(
        'import bpy\n', 'import bpy\n' + preamble, 1
    )

    # Append: smart material fallback (with optional texture map) + overwrite + save .blend
    script_content += _smart_material_script(texture_map, manifest_path=manifest_path)
    script_content += f'''
import os
_blend_path = r"{save_path}"
if os.path.exists(_blend_path):
    os.remove(_blend_path)
    print(f"[OVERWRITE] Deleted existing: {{_blend_path}}")
bpy.ops.wm.save_as_mainfile(filepath=_blend_path)
print(f"[SAVED] {{_blend_path}}")
'''

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    tmp.write(script_content)
    tmp.close()

    try:
        return subprocess.run(
            [str(BLENDER_PATH), "--background", "--python", tmp.name],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        os.unlink(tmp.name)


def run_blender_export_obj(game_path: str, media_name: str, output_obj: Path, body_only: bool = False, interior_only: bool = False, texture_map: dict[str, Path] | None = None) -> subprocess.CompletedProcess:
    script_content = generate_blender_script(game_path, media_name, body_only=body_only, interior_only=interior_only)
    obj_path = str(output_obj).replace("\\", "/")
    manifest_path = str(output_obj.with_suffix(".materials.json")).replace("\\", "/")

    preamble = (
        'import bpy\n'
        'bpy.ops.object.select_all(action="SELECT")\n'
        'bpy.ops.object.delete()\n'
    )
    script_content = script_content.replace(
        'import bpy\n', 'import bpy\n' + preamble, 1
    )

    # Smart material fallback (with optional texture map) then overwrite + export as OBJ
    script_content += _smart_material_script(texture_map, manifest_path=manifest_path)
    script_content += f'''
import os
_obj_path = r"{obj_path}"
_mtl_path = _obj_path.replace(".obj", ".mtl")
for _path in (_obj_path, _mtl_path):
    if os.path.exists(_path):
        os.remove(_path)
        print(f"[OVERWRITE] Deleted existing: {{_path}}")
bpy.ops.wm.obj_export(filepath=_obj_path, export_selected_objects=False)
print(f"[EXPORTED OBJ] {{_obj_path}}")
'''

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    tmp.write(script_content)
    tmp.close()

    try:
        return subprocess.run(
            [str(BLENDER_PATH), "--background", "--python", tmp.name],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        os.unlink(tmp.name)


LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class ConversionSummary:
    total: int
    succeeded: int
    failed: int
    outputs: tuple[Path, ...]


@dataclass(frozen=True)
class CarArchiveMetadata:
    path: Path
    size_bytes: int
    member_count: int
    compressed_bytes: int
    uncompressed_bytes: int
    top_extensions: tuple[tuple[str, int], ...]
    top_folders: tuple[tuple[str, int], ...]
    largest_files: tuple[tuple[str, int], ...]


def _format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ("KB", "MB", "GB", "TB")
    value = float(size_bytes)
    for unit in units:
        value /= 1024.0
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def _shorten_name(name: str, limit: int = 72) -> str:
    if len(name) <= limit:
        return name
    return name[: max(0, limit - 1)] + "…"


@lru_cache(maxsize=256)
def _car_archive_metadata(zip_path: Path) -> CarArchiveMetadata:
    stat = zip_path.stat()
    ext_counts: Counter[str] = Counter()
    folder_counts: Counter[str] = Counter()
    largest_files: list[tuple[str, int]] = []
    member_count = 0
    uncompressed_bytes = 0
    compressed_bytes = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        member_count = len(infos)
        for info in infos:
            filename = info.filename.replace("\\", "/")
            uncompressed_bytes += info.file_size
            compressed_bytes += info.compress_size

            suffix = Path(filename).suffix.lower()
            ext_counts[suffix or "[no ext]"] += 1

            top_folder = filename.split("/", 1)[0]
            if top_folder and top_folder != filename:
                folder_counts[top_folder] += 1

        largest_files = [
            (info.filename.replace("\\", "/"), info.file_size)
            for info in sorted(infos, key=lambda entry: entry.file_size, reverse=True)[:5]
        ]

    return CarArchiveMetadata(
        path=zip_path,
        size_bytes=stat.st_size,
        member_count=member_count,
        compressed_bytes=compressed_bytes,
        uncompressed_bytes=uncompressed_bytes,
        top_extensions=tuple(ext_counts.most_common(5)),
        top_folders=tuple(folder_counts.most_common(5)),
        largest_files=tuple(largest_files),
    )


def _format_archive_metadata(metadata: CarArchiveMetadata) -> str:
    display = car_display_info(metadata.path)
    title = f"{display.friendly_name} [{display.asset_id}]"
    lines = [
        f"{title}",
        f"Archive size: {_format_bytes(metadata.size_bytes)}",
        f"Files: {metadata.member_count:,}",
        f"Packed / unpacked: {_format_bytes(metadata.compressed_bytes)} / {_format_bytes(metadata.uncompressed_bytes)}",
    ]

    if metadata.top_extensions:
        ext_text = ", ".join(f"{ext} x{count}" for ext, count in metadata.top_extensions)
        lines.append(f"Top file types: {ext_text}")
    if metadata.top_folders:
        folder_text = ", ".join(f"{folder} x{count}" for folder, count in metadata.top_folders)
        lines.append(f"Top folders: {folder_text}")
    if metadata.largest_files:
        lines.append("Largest files:")
        for name, size in metadata.largest_files:
            lines.append(f"  - {_shorten_name(name)} ({_format_bytes(size)})")
    return "\n".join(lines)


def _summarize_selected_cars(paths: list[Path]) -> str:
    if not paths:
        return "Select one or more cars to see archive metadata."

    metadata_items = [_car_archive_metadata(path) for path in paths]
    total_size = sum(item.size_bytes for item in metadata_items)
    total_files = sum(item.member_count for item in metadata_items)
    total_unpacked = sum(item.uncompressed_bytes for item in metadata_items)
    total_packed = sum(item.compressed_bytes for item in metadata_items)

    lines = [
        f"Selected cars: {len(paths)}",
        f"Combined size: {_format_bytes(total_size)}",
        f"Combined files: {total_files:,}",
        f"Packed / unpacked: {_format_bytes(total_packed)} / {_format_bytes(total_unpacked)}",
        "",
    ]

    for index, metadata in enumerate(metadata_items[:3], start=1):
        lines.append(f"{index}. {_format_archive_metadata(metadata)}")
        if index != min(3, len(metadata_items)):
            lines.append("")

    if len(metadata_items) > 3:
        lines.append("")
        lines.append(f"+ {len(metadata_items) - 3} more selected car(s)")

    return "\n".join(lines)


def _mode_flags(mode: str) -> tuple[bool, bool]:
    match mode:
        case "full":
            return False, False
        case "body":
            return True, False
        case "interior":
            return False, True
        case _:
            raise ValueError(f"Unknown conversion mode: {mode}")


def _mode_suffix(mode: str) -> str:
    return {"full": "", "body": "_body", "interior": "_interior"}[mode]


def _mode_label(mode: str) -> str:
    return {"full": "Full", "body": "Body Only", "interior": "Interior Only"}[mode]


def find_sandkasse_asset_dir() -> Path | None:
    """Best-effort local Sandkasse asset directory for quick test exports."""
    candidates = (
        Path(__file__).parent.parent / "Sandkasse" / "data" / "forzatech_assets",
        Path(__file__).parent.parent / "Sandkasse" / "data" / "car_assets",
        Path(__file__).parent.parent / "Sandkasse" / "data",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    sandkasse_root = Path(__file__).parent.parent / "Sandkasse"
    if sandkasse_root.exists():
        return sandkasse_root / "data" / "forzatech_assets"
    return None


def resolve_car_zips(queries: list[str] | None = None, all_cars: bool = False) -> list[Path]:
    cars = get_car_display_infos()
    if all_cars:
        return [car.path for car in cars]
    if not queries:
        return []

    selected: list[Path] = []
    seen: set[Path] = set()
    for query in queries:
        query_norm = query.strip().upper()
        if not query_norm:
            continue
        exact = [car for car in cars if car.asset_id.upper() == query_norm or car.friendly_name.upper() == query_norm]
        matches = exact or [car for car in cars if query_norm in car.search_text]
        if not matches:
            raise ValueError(f"No car matched: {query}")
        if len(matches) > 1 and not exact:
            preview = ", ".join(f"{car.friendly_name} [{car.asset_id}]" for car in matches[:8])
            more = "" if len(matches) <= 8 else f", +{len(matches) - 8} more"
            raise ValueError(f"Ambiguous car query '{query}': {preview}{more}")
        for match in matches:
            if match.path not in seen:
                selected.append(match.path)
                seen.add(match.path)
    return selected


def _extract_texture_map(zip_path: Path, media_name: str, workspace: Path, log: LogCallback) -> dict[str, Path]:
    tex_dir = workspace / f"{media_name}_textures"
    try:
        texture_log = lambda msg: log(f"  [{media_name}] {msg}")
        _ensure_shared_material_library_extracted(workspace)
        shared_map = _get_shared_textures(log=texture_log)
        car_map = extract_textures(zip_path, tex_dir, log=texture_log)
        texture_map = {**shared_map, **car_map}
        if texture_map:
            log(f"  [{media_name}] Extracted {len(car_map)} car + {len(shared_map)} shared texture(s)")
        return texture_map
    except Exception as e:
        log(f"  [{media_name}] Texture extraction failed: {e}")
        return {}


def _prepare_shared_tire_model(media_name: str, workspace: Path, log: LogCallback) -> None:
    try:
        tire_log = lambda msg: log(f"  [{media_name}] {msg}")
        _ensure_shared_tire_model_extracted(media_name, workspace, log=tire_log)
    except Exception as e:
        log(f"  [{media_name}] Shared tire extraction failed: {e}")


def _cleanup_extracted_media(workspace: Path, media_name: str) -> None:
    car_media = workspace / "Media" / "Cars" / media_name
    if car_media.exists():
        shutil.rmtree(car_media, True)


def _cleanup_empty_media_root(workspace: Path) -> None:
    media_dir = workspace / "Media"
    if media_dir.exists() and not any(media_dir.rglob("*")):
        shutil.rmtree(media_dir, True)


def run_conversion_job(
    selected: list[Path],
    *,
    kind: str,
    mode: str = "full",
    workspace: Path = WORKSPACE,
    output_dir: Path | None = None,
    workers: int = MAX_WORKERS,
    log: LogCallback | None = None,
) -> ConversionSummary:
    """Run one automation-friendly export job shared by GUI, CLI, and tests."""
    logger = log or print
    workspace = workspace.resolve()
    output_dir = (output_dir or workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not selected:
        logger("No cars selected!")
        return ConversionSummary(0, 0, 0, ())

    if kind not in {"blend", "obj", "bundle", "bundle-zip"}:
        raise ValueError(f"Unknown export kind: {kind}")
    if mode not in {"full", "body", "interior"}:
        raise ValueError(f"Unknown conversion mode: {mode}")

    worker_count = max(1, workers)
    if kind in {"bundle", "bundle-zip"}:
        title = "Extract Bundle Zip" if kind == "bundle-zip" else "Extract Bundle"
    else:
        title = f"Export {kind.upper()} [{_mode_label(mode)}]"
    logger(f"\n=== {title}: {len(selected)} car(s), {worker_count} workers ===")
    logger(f"[GameDB] {game_db_status()}")

    succeeded = 0
    failed = 0
    outputs: list[Path] = []
    lock = threading.Lock()

    def record_success(paths: list[Path]) -> None:
        nonlocal succeeded
        with lock:
            succeeded += 1
            outputs.extend(paths)

    def record_failure() -> None:
        nonlocal failed
        with lock:
            failed += 1

    def process_car(args: tuple[int, Path]) -> None:
        i, zip_path = args
        media_name = zip_path.stem
        logger(f"\n[{i}/{len(selected)}] {media_name}")

        try:
            media_name = extract_car(zip_path, workspace)
        except Exception as e:
            logger(f"  [{media_name}] Extract failed: {e}")
            record_failure()
            return

        _prepare_shared_tire_model(media_name, workspace, logger)
        texture_map = _extract_texture_map(zip_path, media_name, workspace, logger)

        try:
            if kind == "bundle" or kind == "bundle-zip":
                modes = ("full", "body", "interior")
                bundle_dir = output_dir / media_name
                bundle_dir.mkdir(parents=True, exist_ok=True)
                blend_files: list[Path] = []
                zip_path_out = output_dir / f"{media_name}.zip"
                previous_zip_mtime = _prepare_output_path(zip_path_out) if kind == "bundle-zip" else None

                for bundle_mode in modes:
                    body_only, interior_only = _mode_flags(bundle_mode)
                    output_blend = bundle_dir / f"{media_name}{_mode_suffix(bundle_mode)}.blend"
                    previous_mtime = _prepare_output_path(output_blend)
                    result = run_blender_import(
                        str(workspace),
                        media_name,
                        output_blend,
                        body_only=body_only,
                        interior_only=interior_only,
                        texture_map=texture_map or None,
                    )
                    for line in _important_process_lines(result):
                        logger(f"    {line}")
                    if result.returncode == 0 and _is_fresh_output(output_blend, previous_mtime):
                        blend_files.append(output_blend)
                        logger(f"  [{media_name}] [OK] {_mode_label(bundle_mode)}")
                    else:
                        logger(f"  [{media_name}] [FAIL] {_mode_label(bundle_mode)}")
                        for line in _failure_process_lines(result):
                            logger(f"    {line}")

                if len(blend_files) != len(modes):
                    logger(f"  [{media_name}] [FAIL] Bundle incomplete: {len(blend_files)}/{len(modes)} exports succeeded")
                    record_failure()
                    return

                if kind == "bundle-zip":
                    with zipfile.ZipFile(zip_path_out, "w", zipfile.ZIP_DEFLATED) as zf:
                        for blend_file in blend_files:
                            zf.write(blend_file, blend_file.name)
                    shutil.rmtree(bundle_dir, True)
                    if not _is_fresh_output(zip_path_out, previous_zip_mtime):
                        logger(f"  [{media_name}] [FAIL] Bundle zip was not written")
                        record_failure()
                        return
                    logger(f"  [{media_name}] [BUNDLE] {zip_path_out}")
                    record_success([zip_path_out])
                else:
                    logger(f"  [{media_name}] [BUNDLE] {bundle_dir}")
                    record_success(blend_files)
                return

            body_only, interior_only = _mode_flags(mode)
            suffix = _mode_suffix(mode)
            if kind == "obj":
                output_obj = output_dir / f"{media_name}{suffix}.obj"
                previous_obj_mtime = _prepare_output_path(output_obj)
                previous_mtl_mtime = _prepare_output_path(output_obj.with_suffix(".mtl"))
                if previous_obj_mtime is not None or previous_mtl_mtime is not None:
                    logger(f"  [{media_name}] [OVERWRITE] Removed stale OBJ output before export")
                result = run_blender_export_obj(
                    str(workspace),
                    media_name,
                    output_obj,
                    body_only=body_only,
                    interior_only=interior_only,
                    texture_map=texture_map or None,
                )
                for line in _important_process_lines(result):
                    logger(f"    {line}")
                if result.returncode == 0 and _is_fresh_output(output_obj, previous_obj_mtime):
                    logger(f"  [{media_name}] [OK]")
                    record_success([output_obj])
                else:
                    logger(f"  [{media_name}] [FAIL]")
                    for line in _failure_process_lines(result):
                        logger(f"    {line}")
                    record_failure()
                return

            output_blend = output_dir / f"{media_name}{suffix}.blend"
            previous_mtime = _prepare_output_path(output_blend)
            if previous_mtime is not None:
                logger(f"  [{media_name}] [OVERWRITE] Removed stale output before export")
            result = run_blender_import(
                str(workspace),
                media_name,
                output_blend,
                body_only=body_only,
                interior_only=interior_only,
                texture_map=texture_map or None,
            )
            for line in _important_process_lines(result):
                logger(f"    {line}")
            if result.returncode == 0 and _is_fresh_output(output_blend, previous_mtime):
                logger(f"  [{media_name}] [OK]")
                record_success([output_blend])
            else:
                logger(f"  [{media_name}] [FAIL]")
                for line in _failure_process_lines(result):
                    logger(f"    {line}")
                record_failure()
        except subprocess.TimeoutExpired:
            logger(f"  [{media_name}] [TIMEOUT]")
            record_failure()
        except Exception as e:
            logger(f"  [{media_name}] [ERROR] {e}")
            record_failure()
        finally:
            _cleanup_extracted_media(workspace, media_name)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(process_car, enumerate(selected, 1)))

    _cleanup_empty_media_root(workspace)
    logger(
        f"\nDone! {succeeded} succeeded, {failed} failed out of {len(selected)}."
        + (f"\nOutput: {output_dir}" if succeeded > 0 else "")
    )
    return ConversionSummary(len(selected), succeeded, failed, tuple(outputs))


class ForzaTechApp(App):
    CSS = """
    #main { height: 1fr; }
    #left-panel { width: 2fr; }
    #search { margin-bottom: 1; }
    #car-list { height: 1fr; }
    #sidebar { width: 1fr; padding: 1; }
    #btn-grid { grid-size: 2; grid-gutter: 1; }
    #btn-grid Button { width: 100%; }
    #log-panel { height: 1fr; border: solid green; }
    #status { padding: 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("a", "select_all", "Select All"),
        ("n", "select_none", "Select None"),
    ]

    def __init__(self):
        super().__init__()
        self._log_lines: list[str] = []
        self._cars: list[CarDisplayInfo] = get_car_display_infos()

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)
        self.query_one("#log-panel", Log).write_line(msg)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield Input(placeholder="Search cars...", id="search")
                selections = [Selection(car.friendly_name, car.path, False) for car in self._cars]
                yield SelectionList[Path](*selections, id="car-list")
            with Vertical(id="sidebar"):
                yield Label(f"Cars found: {len(self._cars)}", id="status")
                with Grid(id="btn-grid"):
                    yield Button("Convert (Full)", id="convert", variant="success")
                    yield Button("Convert (Body Only)", id="convert-body", variant="primary")
                    yield Button("Convert (Interior Only)", id="convert-interior", variant="primary")
                    yield Button("Export OBJ", id="export-obj", variant="success")
                    yield Button("Extract Bundle", id="extract-bundle", variant="warning")
                    yield Button("Extract Bundle Zip", id="extract-bundle-zip", variant="warning")
                    yield Button("Select All", id="btn-all", variant="default")
                    yield Button("Select None", id="btn-none", variant="warning")
                yield Button("Save Log", id="btn-save-log", variant="default")
                yield Log(id="log-panel", highlight=True, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "ForzaTech Wrapper"
        self.sub_title = f"Forza: {FORZA_PATH}"
        self._log(f"Forza path: {FORZA_PATH}")
        self._log(f"Blender path: {BLENDER_PATH}")
        self._log(f"Script: {SCRIPT_PATH}")
        self._log(f"Output dir: {WORKSPACE}")
        self._log(f"Workers: {MAX_WORKERS}")
        if not CARS_PATH.exists():
            self._log("[ERROR] Cars path not found!")
        if not BLENDER_PATH.exists():
            self._log("[ERROR] Blender executable not found!")
        if not SCRIPT_PATH.exists():
            self._log("[ERROR] carbin_importer.py not found!")

    def on_selection_list_selected_changed(self) -> None:
        sel_list = self.query_one("#car-list", SelectionList)
        self.query_one("#status", Label).update(f"Selected: {len(sel_list.selected)}")

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().upper()
        sel_list = self.query_one("#car-list", SelectionList)
        sel_list.clear_options()
        filtered = [car for car in self._cars if query in car.search_text] if query else self._cars
        sel_list.add_options([Selection(car.friendly_name, car.path, False) for car in filtered])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "convert":
            self.do_convert(body_only=False)
        elif event.button.id == "convert-body":
            self.do_convert(body_only=True)
        elif event.button.id == "convert-interior":
            self.do_convert(interior_only=True)
        elif event.button.id == "extract-bundle":
            self.do_extract_bundle(as_zip=False)
        elif event.button.id == "extract-bundle-zip":
            self.do_extract_bundle(as_zip=True)
        elif event.button.id == "export-obj":
            self.do_export_obj()
        elif event.button.id == "btn-all":
            self.action_select_all()
        elif event.button.id == "btn-none":
            self.action_select_none()
        elif event.button.id == "btn-save-log":
            self.save_log()

    def save_log(self) -> None:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        log_file = WORKSPACE / "log.txt"
        log_file.write_text("\n".join(self._log_lines), encoding="utf-8")
        self._log(f"[Log saved to: {log_file}]")

    def action_select_all(self) -> None:
        self.query_one("#car-list", SelectionList).select_all()

    def action_select_none(self) -> None:
        self.query_one("#car-list", SelectionList).deselect_all()

    def do_convert(self, body_only: bool = False, interior_only: bool = False) -> None:
        mode = "body" if body_only else "interior" if interior_only else "full"
        self._run_conversion(list(self.query_one("#car-list", SelectionList).selected), "blend", mode)

    def do_export_obj(self) -> None:
        self._run_conversion(list(self.query_one("#car-list", SelectionList).selected), "obj")

    def do_extract_bundle(self, as_zip: bool = False) -> None:
        kind = "bundle-zip" if as_zip else "bundle"
        self._run_conversion(list(self.query_one("#car-list", SelectionList).selected), kind)

    @work(thread=True)
    def _run_conversion(self, selected: list[Path], kind: str, mode: str = "full") -> None:
        def log(message: str) -> None:
            self.call_from_thread(self._log, message)

        try:
            run_conversion_job(selected, kind=kind, mode=mode, log=log)
        except Exception as exc:
            log(f"[ERROR] Conversion failed: {exc}")


def run_pyside_gui() -> int:
    try:
        from PySide6.QtCore import QObject, QThread, Signal, Qt
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import (
            QApplication,
            QAbstractItemView,
            QButtonGroup,
            QCheckBox,
            QComboBox,
            QFileDialog,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QRadioButton,
            QSplitter,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        print(f"PySide6 is not installed: {exc}", file=sys.stderr)
        print("Run `uv sync`, or use `uv run python main.py tui` for the old terminal UI.", file=sys.stderr)
        return 1

    class ConversionSignals(QObject):
        log = Signal(str)
        done = Signal(object)
        error = Signal(str)

    class ConversionThread(QThread):
        def __init__(
            self,
            selected: list[Path],
            *,
            kind: str,
            mode: str,
            workspace: Path,
            output_dir: Path,
            workers: int,
        ) -> None:
            super().__init__()
            self.selected = selected
            self.kind = kind
            self.mode = mode
            self.workspace = workspace
            self.output_dir = output_dir
            self.workers = workers
            self.signals = ConversionSignals()

        def run(self) -> None:
            try:
                summary = run_conversion_job(
                    self.selected,
                    kind=self.kind,
                    mode=self.mode,
                    workspace=self.workspace,
                    output_dir=self.output_dir,
                    workers=self.workers,
                    log=self.signals.log.emit,
                )
                self.signals.done.emit(summary)
            except Exception as exc:
                self.signals.error.emit(str(exc))

    class ForzaTechWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("ForzaTech Wrapper")
            self.resize(1180, 760)
            self._cars = get_car_display_infos()
            self._worker: ConversionThread | None = None
            self._sort_descending = False

            central = QWidget()
            root = QVBoxLayout(central)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)
            self.setCentralWidget(central)

            header = QHBoxLayout()
            title = QLabel("ForzaTech Wrapper")
            title.setStyleSheet("font-size: 20px; font-weight: 700;")
            self.status_label = QLabel(f"{len(self._cars)} cars found")
            self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            header.addWidget(title)
            header.addWidget(self.status_label, 1)
            root.addLayout(header)

            splitter = QSplitter(Qt.Horizontal)
            root.addWidget(splitter, 1)

            left = QWidget()
            left_layout = QVBoxLayout(left)
            left_layout.setContentsMargins(0, 0, 8, 0)
            self.search = QLineEdit()
            self.search.setPlaceholderText("Search name, make, model, year, or asset id...")
            sort_row = QHBoxLayout()
            self.sort_combo = QComboBox()
            self.sort_combo.addItem("Friendly Name", "friendly")
            self.sort_combo.addItem("Make", "make")
            self.sort_combo.addItem("Model", "model")
            self.sort_combo.addItem("Year", "year")
            self.sort_combo.addItem("Asset ID", "asset")
            self.sort_dir_btn = QPushButton("Asc")
            self.sort_dir_btn.setCheckable(True)
            sort_row.addWidget(QLabel("Sort"))
            sort_row.addWidget(self.sort_combo, 1)
            sort_row.addWidget(self.sort_dir_btn)
            self.car_table = QTableWidget(0, 5)
            self.car_table.setHorizontalHeaderLabels(["Friendly Name", "Make", "Model", "Year", "Asset ID"])
            self.car_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.car_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.car_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.car_table.setAlternatingRowColors(True)
            self.car_table.verticalHeader().setVisible(False)
            self.car_table.horizontalHeader().setStretchLastSection(True)
            self.car_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.car_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.car_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.car_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
            self.car_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
            self.car_table.setToolTip("Select one or more cars. The asset id column is the original ForzaTech archive name.")
            left_layout.addWidget(self.search)
            left_layout.addLayout(sort_row)
            left_layout.addWidget(self.car_table, 1)
            splitter.addWidget(left)

            right = QWidget()
            right_layout = QVBoxLayout(right)
            right_layout.setContentsMargins(8, 0, 0, 0)
            right_layout.setSpacing(8)
            splitter.addWidget(right)
            splitter.setSizes([430, 750])

            mode_row = QHBoxLayout()
            self.mode_group = QButtonGroup(self)
            self.mode_full = QRadioButton("Full")
            self.mode_body = QRadioButton("Body")
            self.mode_interior = QRadioButton("Interior")
            self.mode_full.setChecked(True)
            self.mode_full.setToolTip("Import every available part for the selected car.")
            self.mode_body.setToolTip("Import only exterior body panels and other body-focused parts.")
            self.mode_interior.setToolTip("Import only interior and cabin parts like dash, seats, and console.")
            for button in (self.mode_full, self.mode_body, self.mode_interior):
                self.mode_group.addButton(button)
                mode_row.addWidget(button)
            mode_row.addStretch(1)
            right_layout.addLayout(mode_row)

            action_row = QHBoxLayout()
            self.convert_btn = QPushButton("Convert Blend")
            self.obj_btn = QPushButton("Export OBJ")
            self.bundle_btn = QPushButton("Bundle")
            self.bundle_zip_btn = QPushButton("Bundle Zip")
            self.convert_btn.setToolTip("Convert the selected cars to .blend files in the chosen output folder.")
            self.obj_btn.setToolTip("Export the selected cars as .obj plus .mtl files.")
            self.bundle_btn.setToolTip("Export full, body, and interior .blend files into a folder per car.")
            self.bundle_zip_btn.setToolTip("Export all three .blend variants and package them into one zip per car.")
            for button in (self.convert_btn, self.obj_btn, self.bundle_btn, self.bundle_zip_btn):
                action_row.addWidget(button)
            right_layout.addLayout(action_row)

            selection_row = QHBoxLayout()
            self.select_all_btn = QPushButton("Select All")
            self.clear_btn = QPushButton("Clear")
            self.save_log_btn = QPushButton("Save Log")
            self.select_all_btn.setToolTip("Select every car currently visible in the filtered list.")
            self.clear_btn.setToolTip("Clear the current selection.")
            self.save_log_btn.setToolTip("Write the current log panel contents to workspace/log.txt.")
            selection_row.addWidget(self.select_all_btn)
            selection_row.addWidget(self.clear_btn)
            selection_row.addStretch(1)
            selection_row.addWidget(self.save_log_btn)
            right_layout.addLayout(selection_row)

            output_row = QHBoxLayout()
            self.output_edit = QLineEdit(str(WORKSPACE))
            self.output_edit.setPlaceholderText("Output directory")
            self.browse_btn = QPushButton("Browse")
            self.sandkasse_box = QCheckBox("Sandkasse test output")
            self.search.setToolTip("Filter cars by name as you type.")
            self.sort_combo.setToolTip("Choose which car field controls the row order.")
            self.sort_dir_btn.setToolTip("Toggle ascending or descending sort.")
            self.output_edit.setToolTip("Destination folder for converted files.")
            self.browse_btn.setToolTip("Choose a custom output folder.")
            self.sandkasse_box.setToolTip("Redirect output to a nearby Sandkasse checkout when available.")
            output_row.addWidget(QLabel("Output"))
            output_row.addWidget(self.output_edit, 1)
            output_row.addWidget(self.browse_btn)
            output_row.addWidget(self.sandkasse_box)
            right_layout.addLayout(output_row)

            self.details_label = QLabel("Selected car metadata")
            self.details_label.setStyleSheet("font-size: 11px; font-weight: 700;")
            self.details_panel = QPlainTextEdit()
            self.details_panel.setObjectName("details-panel")
            self.details_panel.setReadOnly(True)
            self.details_panel.setLineWrapMode(QPlainTextEdit.WidgetWidth)
            self.details_panel.setMaximumHeight(210)
            self.details_panel.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
            self.details_panel.setToolTip("Quick metadata for the currently selected car(s).")
            right_layout.addWidget(self.details_label)
            right_layout.addWidget(self.details_panel)

            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
            right_layout.addWidget(self.log, 1)

            self.search.textChanged.connect(self._populate_cars)
            self.sort_combo.currentIndexChanged.connect(self._populate_cars)
            self.sort_dir_btn.toggled.connect(self._set_sort_direction)
            self.car_table.itemSelectionChanged.connect(self._update_status)
            self.car_table.horizontalHeader().sectionClicked.connect(self._sort_by_header)
            self.select_all_btn.clicked.connect(self.car_table.selectAll)
            self.clear_btn.clicked.connect(self.car_table.clearSelection)
            self.browse_btn.clicked.connect(self._browse_output)
            self.save_log_btn.clicked.connect(self._save_log)
            self.sandkasse_box.toggled.connect(self._toggle_sandkasse_output)
            self.convert_btn.clicked.connect(lambda: self._start("blend"))
            self.obj_btn.clicked.connect(lambda: self._start("obj"))
            self.bundle_btn.clicked.connect(lambda: self._start("bundle"))
            self.bundle_zip_btn.clicked.connect(lambda: self._start("bundle-zip"))

            self._populate_cars()
            self._log(f"Forza path: {FORZA_PATH}")
            self._log(f"Blender path: {BLENDER_PATH}")
            self._log(f"Output dir: {WORKSPACE}")
            if not CARS_PATH.exists():
                self._log("[ERROR] Cars path not found")
            if not BLENDER_PATH.exists():
                self._log("[ERROR] Blender executable not found")

        def _populate_cars(self) -> None:
            query = self.search.text().strip().upper()
            visible = [car for car in self._cars if not query or query in car.search_text]
            visible.sort(key=self._sort_key, reverse=self._sort_descending)

            self.car_table.setRowCount(0)
            self.car_table.setRowCount(len(visible))
            for row, car in enumerate(visible):
                values = (car.friendly_name, car.make, car.model, car.year_label, car.asset_id)
                tooltip = f"{car.friendly_name}\nAsset ID: {car.asset_id}"
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(tooltip)
                    if column == 0:
                        item.setData(Qt.UserRole, car.path)
                    self.car_table.setItem(row, column, item)
            self._update_status()

        def _selected_cars(self) -> list[Path]:
            selected: list[Path] = []
            seen: set[Path] = set()
            for index in self.car_table.selectionModel().selectedRows():
                item = self.car_table.item(index.row(), 0)
                if item is None:
                    continue
                path = item.data(Qt.UserRole)
                if path not in seen:
                    selected.append(path)
                    seen.add(path)
            return selected

        def _sort_key(self, car: CarDisplayInfo) -> tuple[object, ...]:
            sort_part = self.sort_combo.currentData()
            if sort_part == "make":
                return (car.make.lower(), car.model.lower(), car.year or 0, car.asset_id.lower())
            if sort_part == "model":
                return (car.model.lower(), car.make.lower(), car.year or 0, car.asset_id.lower())
            if sort_part == "year":
                return (car.year is None, car.year or 0, car.make.lower(), car.model.lower())
            if sort_part == "asset":
                return (car.asset_id.lower(),)
            return (car.friendly_name.lower(), car.asset_id.lower())

        def _set_sort_direction(self, descending: bool) -> None:
            self._sort_descending = descending
            self.sort_dir_btn.setText("Desc" if descending else "Asc")
            self._populate_cars()

        def _sort_by_header(self, column: int) -> None:
            column_to_sort = {0: "friendly", 1: "make", 2: "model", 3: "year", 4: "asset"}
            sort_part = column_to_sort.get(column, "friendly")
            current = self.sort_combo.currentData()
            if current == sort_part:
                self.sort_dir_btn.setChecked(not self.sort_dir_btn.isChecked())
                return
            index = self.sort_combo.findData(sort_part)
            if index >= 0:
                self.sort_combo.setCurrentIndex(index)

        def _mode(self) -> str:
            if self.mode_body.isChecked():
                return "body"
            if self.mode_interior.isChecked():
                return "interior"
            return "full"

        def _set_busy(self, busy: bool) -> None:
            for widget in (
                self.convert_btn,
                self.obj_btn,
                self.bundle_btn,
                self.bundle_zip_btn,
                self.select_all_btn,
                self.clear_btn,
                self.browse_btn,
                self.sort_combo,
                self.sort_dir_btn,
                self.search,
            ):
                widget.setEnabled(not busy)

        def _start(self, kind: str) -> None:
            selected = self._selected_cars()
            if not selected:
                QMessageBox.information(self, "ForzaTech Wrapper", "Select one or more cars first.")
                return
            output_dir = Path(self.output_edit.text()).expanduser()
            self._set_busy(True)
            self._worker = ConversionThread(
                selected,
                kind=kind,
                mode=self._mode(),
                workspace=WORKSPACE,
                output_dir=output_dir,
                workers=MAX_WORKERS,
            )
            self._worker.signals.log.connect(self._log)
            self._worker.signals.done.connect(self._done)
            self._worker.signals.error.connect(self._error)
            self._worker.start()

        def _done(self, summary: ConversionSummary) -> None:
            self._set_busy(False)
            self._log(f"[done] {summary.succeeded}/{summary.total} succeeded")

        def _error(self, message: str) -> None:
            self._set_busy(False)
            self._log(f"[error] {message}")
            QMessageBox.critical(self, "ForzaTech Wrapper", message)

        def _browse_output(self) -> None:
            directory = QFileDialog.getExistingDirectory(self, "Choose output directory", self.output_edit.text())
            if directory:
                self.output_edit.setText(directory)

        def _toggle_sandkasse_output(self, checked: bool) -> None:
            if not checked:
                self.output_edit.setText(str(WORKSPACE))
                return
            target = find_sandkasse_asset_dir()
            if target is None:
                self._log("[Sandkasse] Could not find a neighboring Sandkasse checkout.")
                self.sandkasse_box.setChecked(False)
                return
            target.mkdir(parents=True, exist_ok=True)
            self.output_edit.setText(str(target))
            self._log(f"[Sandkasse] Test exports will go to: {target}")

        def _save_log(self) -> None:
            WORKSPACE.mkdir(parents=True, exist_ok=True)
            log_file = WORKSPACE / "log.txt"
            log_file.write_text(self.log.toPlainText(), encoding="utf-8")
            self._log(f"[Log saved to: {log_file}]")

        def _update_status(self) -> None:
            self.status_label.setText(f"{self.car_table.rowCount()} shown, {len(self._selected_cars())} selected")
            self.details_panel.setPlainText(_summarize_selected_cars(self._selected_cars()))

        def _log(self, message: str) -> None:
            self.log.appendPlainText(message)

    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QWidget { background: #17191d; color: #e7eaf0; font-size: 10pt; }
        QLineEdit, QTableWidget, QPlainTextEdit, QComboBox {
            background: #101216; border: 1px solid #303640; border-radius: 6px; padding: 6px;
        }
        QTableWidget {
            gridline-color: #29313a;
            alternate-background-color: #141820;
            selection-background-color: #2f6f9f;
            selection-color: #ffffff;
        }
        QHeaderView::section {
            background: #20252c;
            border: 0;
            border-right: 1px solid #303640;
            border-bottom: 1px solid #303640;
            padding: 6px;
            font-weight: 700;
        }
        QComboBox::drop-down {
            border: 0;
            width: 22px;
        }
        QPushButton {
            background: #2f6f9f; border: none; border-radius: 6px; padding: 8px 12px; font-weight: 600;
        }
        QPushButton:hover { background: #3d82b8; }
        QPushButton:disabled { background: #2b3037; color: #7d8591; }
        QRadioButton, QCheckBox {
            spacing: 6px;
            padding: 3px 8px 3px 2px;
            border: 1px solid transparent;
            border-radius: 6px;
        }
        QRadioButton:hover, QCheckBox:hover { background: #1d2229; border-color: #2f3945; }
        QRadioButton::indicator {
            width: 15px;
            height: 15px;
            border-radius: 8px;
            border: 2px solid #9aa6b2;
            background: #0c0f13;
        }
        QRadioButton::indicator:hover { border-color: #d0dae6; background: #151922; }
        QRadioButton::indicator:checked {
            border-color: #7dc0ff;
            background: #2f6f9f;
        }
        QRadioButton::indicator:checked:hover {
            border-color: #a6d7ff;
            background: #3d82b8;
        }
        QToolTip {
            color: #eef3f8;
            background-color: #1d2229;
            border: 1px solid #495566;
            padding: 6px;
        }
        QPlainTextEdit#details-panel {
            background: #11151a;
            border: 1px solid #3a4450;
        }
        QSplitter::handle { background: #242830; }
        """
    )
    window = ForzaTechWindow()
    window.show()
    return app.exec()


def run_tui() -> None:
    app = ForzaTechApp()
    app.run()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ForzaTech Wrapper GUI and automation CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="Launch the PySide6 desktop UI")
    subparsers.add_parser("tui", help="Launch the legacy Textual terminal UI")

    list_parser = subparsers.add_parser("list", help="List available car assets")
    list_parser.add_argument("--filter", default="", help="Only show cars containing this text")

    convert = subparsers.add_parser("convert", help="Convert one or more cars")
    convert.add_argument("--car", action="append", default=[], help="Car zip stem or unique substring. Can be repeated.")
    convert.add_argument("--all", action="store_true", help="Convert every available car")
    convert.add_argument("--mode", choices=["full", "body", "interior"], default="full")
    convert.add_argument("--format", choices=["blend", "obj", "bundle", "bundle-zip"], default="blend")
    convert.add_argument("--workspace", type=Path, default=WORKSPACE)
    convert.add_argument("--output-dir", type=Path)
    convert.add_argument("--workers", type=int, default=MAX_WORKERS)
    convert.add_argument("--sandkasse", action="store_true", help="Write outputs to a neighboring Sandkasse data asset folder")

    test_car = subparsers.add_parser("test-car", help="Run a full test extraction/conversion for one car")
    test_car.add_argument("car", help="Car zip stem or unique substring")
    test_car.add_argument("--workspace", type=Path, default=WORKSPACE)
    test_car.add_argument("--output-dir", type=Path)
    test_car.add_argument("--workers", type=int, default=1)
    test_car.add_argument("--sandkasse", action="store_true", default=True)

    return parser


def _resolve_output_dir(args: argparse.Namespace) -> Path | None:
    explicit_output = getattr(args, "output_dir", None)
    if explicit_output is not None:
        return explicit_output
    if getattr(args, "sandkasse", False):
        sandkasse_dir = find_sandkasse_asset_dir()
        if sandkasse_dir is None:
            raise ValueError("Could not find a neighboring Sandkasse checkout for --sandkasse output")
        return sandkasse_dir
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "gui"):
        return run_pyside_gui()
    if args.command == "tui":
        run_tui()
        return 0
    if args.command == "list":
        query = args.filter.strip().upper()
        for car in get_car_display_infos():
            if not query or query in car.search_text:
                print(f"{car.friendly_name}\t{car.asset_id}")
        return 0
    if args.command == "convert":
        try:
            selected = resolve_car_zips(args.car, all_cars=args.all)
            output_dir = _resolve_output_dir(args)
            summary = run_conversion_job(
                selected,
                kind=args.format,
                mode=args.mode,
                workspace=args.workspace,
                output_dir=output_dir,
                workers=args.workers,
            )
            return 0 if summary.failed == 0 else 2
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command == "test-car":
        try:
            selected = resolve_car_zips([args.car])
            output_dir = args.output_dir or find_sandkasse_asset_dir()
            summary = run_conversion_job(
                selected,
                kind="bundle",
                mode="full",
                workspace=args.workspace,
                output_dir=output_dir,
                workers=args.workers,
            )
            return 0 if summary.failed == 0 else 2
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
