"""Small, private-data-free checks for the GameDB import boundary.

The importer normally runs inside Blender.  Execute only its isolated GameDB
lookup block here so the actual generated SQL can be checked with SQLite.
"""

import ast
from contextlib import closing
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import main


class _GameDbFixture:
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "fixture.slt"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE Data_Car (
                    Id INTEGER PRIMARY KEY, MediaName TEXT, Year INTEGER,
                    FrontTireWidthMM INTEGER, FrontTireAspect INTEGER,
                    FrontWheelDiameterIN INTEGER, RearTireWidthMM INTEGER,
                    RearTireAspect INTEGER, RearWheelDiameterIN INTEGER
                );
                CREATE TABLE List_UpgradeTireCompound (
                    Ordinal INTEGER, TireModelName TEXT, IsStock INTEGER
                );
                CREATE TABLE List_UpgradeCarBody (
                    Ordinal INTEGER, CarBodyID INTEGER, IsStock INTEGER
                );
                CREATE TABLE Data_CarBody (
                    Id INTEGER PRIMARY KEY, ModelWheelbase REAL,
                    ModelFrontTrackOuter REAL, ModelRearTrackOuter REAL,
                    ModelFrontStockRideHeight REAL, ModelRearStockRideHeight REAL,
                    BottomCenterWheelbasePosx REAL,
                    BottomCenterWheelbasePosy REAL,
                    BottomCenterWheelbasePosZ REAL
                );
                """
            )
            connection.executemany(
                "INSERT INTO Data_Car VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "CAR_A_26", 2025, 245, 40, 18, 265, 40, 18),
                    # LIKE treats '_' as a wildcard.  This row must not win.
                    (2, "CAR_AB26", 2026, 999, 90, 24, 999, 90, 24),
                    (3, "FER_LaFerrari_13", 2013, 265, 30, 19, 345, 30, 20),
                    (4, "CAR_INCOMPLETE_26", None, None, 40, 18, 265, 40, 18),
                    (5, "AH_3000_65", 1965, 165, 75, 15, 165, 75, 15),
                ],
            )
            connection.executemany(
                "INSERT INTO List_UpgradeTireCompound VALUES (?, ?, 1)",
                [(1, "Street"), (2, "Race"), (3, "Semi_Slick"), (4, "Street"), (5, "Street")],
            )
            connection.executemany(
                "INSERT INTO List_UpgradeCarBody VALUES (?, ?, ?)",
                [(1, 20, 1), (1, 21, 0), (2, 10, 1), (3, 30, 1), (4, 40, 1), (5, 50, 1)],
            )
            connection.executemany(
                "INSERT INTO Data_CarBody VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (10, 9.9, 1.6, 1.6, 0.1, 0.1, 0, 0, 0),
                    (20, 2.7, 1.6, 1.6, 0.1, 0.1, 0, 0, 0),
                    (21, 2.8, 1.6, 1.6, 0.1, 0.1, 0, 0, 0),
                    (30, 2.65, 1.6, 1.6, 0.1, 0.1, 0, 0, 0),
                    (40, 2.7, 1.6, 1.6, 0.1, 0.1, 0, 0, 0),
                    (50, 2.3, 1.5, 1.5, 0.1, 0.1, 0, 0, 0),
                ],
            )
            connection.commit()



class GameDbImportTests(_GameDbFixture, unittest.TestCase):
    def _generated_script(self, media_name: str, *, with_db: bool) -> str:
        db_path = self.db_path if with_db else None
        with patch.object(main, "_readable_game_db_path", return_value=db_path):
            return main.generate_blender_script(self.temp_dir.name, media_name)

    @staticmethod
    def _top_level_setting(script: str, name: str):
        for node in ast.parse(script).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return ast.literal_eval(node.value)
        raise AssertionError(f"Generated script has no top-level {name}")

    def _run_importer_lookup(self, media_name: str, car_body_id: int | None = None) -> dict:
        """Execute the generated importer's DB lookup without loading Blender."""
        script = self._generated_script(media_name, with_db=True)
        module = ast.parse(script)
        blocks = [
            node for node in module.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "use_db"
        ]
        self.assertEqual(len(blocks), 1)
        lookup = ast.fix_missing_locations(ast.Module(body=blocks, type_ignores=[]))
        namespace = {
            "use_db": True,
            "sqlite3": sqlite3,
            "closing": closing,
            "pathlib": __import__("pathlib"),
            "db_path": str(self.db_path),
            "media_name": media_name,
            "car_body_id": car_body_id,
        }
        exec(compile(lookup, "generated_carbin_lookup.py", "exec"), namespace)
        return namespace

    def test_stock_compound_precedes_manual_override_and_uses_exact_name(self) -> None:
        with patch.object(main, "_readable_game_db_path", return_value=self.db_path):
            self.assertEqual(main._tire_model_name_for_import("FER_LaFerrari_13"), ("Semi_Slick", "GameDB"))
            self.assertEqual(main._tire_model_name_for_import("CAR_A_26"), ("Street", "GameDB"))
            self.assertEqual(main._tire_model_name_for_import("CAR_INCOMPLETE_26"), ("WET_c", "default"))

    def test_generated_importer_uses_exact_car_and_selected_body(self) -> None:
        default = self._run_importer_lookup("CAR_A_26")
        self.assertEqual(default["TireModelName"], "Street")
        self.assertEqual(default["RearTireWidthMM"], 265)
        self.assertEqual(default["ModelWheelbase"], 2.7)

        selected = self._run_importer_lookup("CAR_A_26", car_body_id=21)
        self.assertEqual(selected["ModelWheelbase"], 2.8)

        case_changed = self._run_importer_lookup("car_a_26")
        self.assertEqual(case_changed["TireModelName"], "Street")

    def test_missing_car_body_is_reported_instead_of_using_another(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run_importer_lookup("CAR_A_26", car_body_id=999)

    def test_catalog_uses_db_year_with_filename_fallback(self) -> None:
        archives = [
            Path("CAR_A_26.zip"),
            Path("AH_3000_65.zip"),
            Path("CAR_INCOMPLETE_26.zip"),
            Path("CAR_MISSING_99.zip"),
        ]
        with (
            patch.object(main, "get_car_zips", return_value=archives),
            patch.object(main, "_readable_game_db_path", return_value=self.db_path),
        ):
            catalog = {car.asset_id: car for car in main.get_car_display_infos()}

        self.assertEqual(catalog["CAR_A_26"].year, 2025)
        self.assertEqual(catalog["AH_3000_65"].year, 1965)
        self.assertEqual(catalog["CAR_INCOMPLETE_26"].year, 2026)
        self.assertEqual(catalog["CAR_MISSING_99"].year, 1999)
        for archive in archives:
            filename_info = main.car_display_info(archive)
            catalog_info = catalog[archive.stem]
            self.assertEqual((catalog_info.make, catalog_info.model),
                             (filename_info.make, filename_info.model))

        with (
            patch.object(main, "get_car_zips", return_value=archives),
            patch.object(main, "_readable_game_db_path", return_value=None),
        ):
            no_db = {car.asset_id: car for car in main.get_car_display_infos()}
        self.assertEqual(no_db["AH_3000_65"].year, 1965)
        self.assertEqual(no_db["CAR_A_26"].year, 2026)
        self.assertEqual(main.car_display_info(Path("AST_DB5_64.zip")).year, 1964)

    def test_generated_modes_keep_database_and_compatibility_paths(self) -> None:
        db_script = self._generated_script("FER_LaFerrari_13", with_db=True)
        self.assertIs(self._top_level_setting(db_script, "use_db"), True)
        self.assertEqual(self._top_level_setting(db_script, "suspension_transform_type"), 2)
        self.assertEqual(self._top_level_setting(db_script, "db_path"), str(self.db_path).replace("\\", "/"))
        compile(db_script, "generated_carbin_db.py", "exec")

        fallback_script = self._generated_script("FER_LaFerrari_13", with_db=False)
        self.assertIs(self._top_level_setting(fallback_script, "use_db"), False)
        self.assertEqual(self._top_level_setting(fallback_script, "suspension_transform_type"), 1)
        self.assertEqual(self._top_level_setting(fallback_script, "TireModelName"), "WET_c")
        self.assertEqual(self._top_level_setting(fallback_script, "RearTireWidthMM"), 345)
        compile(fallback_script, "generated_carbin_fallback.py", "exec")

        incomplete_script = self._generated_script("CAR_INCOMPLETE_26", with_db=True)
        self.assertIs(self._top_level_setting(incomplete_script, "use_db"), False)
        self.assertEqual(self._top_level_setting(incomplete_script, "suspension_transform_type"), 1)
        with patch.object(main, "_readable_game_db_path", return_value=self.db_path):
            mesh_name, mesh_source = main._tire_model_name_for_import("CAR_INCOMPLETE_26")
        self.assertEqual(mesh_source, "default")
        self.assertEqual(mesh_name, self._top_level_setting(incomplete_script, "TireModelName"))


class GameDbResolverTests(_GameDbFixture, unittest.TestCase):
    def test_sqlite_file_without_consumer_schema_is_rejected(self) -> None:
        unrelated = Path(self.temp_dir.name) / "unrelated.slt"
        with closing(sqlite3.connect(unrelated)) as connection:
            connection.execute("CREATE TABLE unrelated (value INTEGER)")
            connection.commit()
        self.assertFalse(main._is_readable_sqlite_db(unrelated))
        self.assertTrue(main._is_readable_sqlite_db(self.db_path))

    def test_cache_requires_source_bound_receipt(self) -> None:
        source = Path(self.temp_dir.name) / "encrypted.slt"
        source.write_bytes(b"encrypted GameDB fixture")
        cache_dir = Path(self.temp_dir.name) / "cache"
        with (
            patch.object(main, "DECRYPTED_GAME_DB_DIR", cache_dir),
            patch.object(main, "_game_db_source_candidates", return_value=(source,)),
            patch.object(main, "_decrypt_game_db_with_fh6_static", return_value=None),
            patch.object(main, "_decrypt_game_db_with_crypto_tool", return_value=None),
            patch.object(main, "_game_db_path_cache", main._GAME_DB_CACHE_MISS),
            patch.object(main, "_game_db_path_cache_key", None),
        ):
            cache = main._decrypted_game_db_cache_path(source)
            cache.parent.mkdir(parents=True)
            shutil.copyfile(self.db_path, cache)
            receipt_path = main._game_db_cache_receipt_path(cache)

            self.assertIsNone(main._readable_game_db_path())

            stat = source.stat()
            receipt = {
                "method": "fixture",
                "source": {
                    "path": str(source.resolve()),
                    "bytes": stat.st_size,
                    "mtimeNs": stat.st_mtime_ns,
                    "sha256": main._sha256_file(source),
                },
                "output": {
                    "bytes": cache.stat().st_size,
                    "sha256": main._sha256_file(cache),
                },
            }
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(main._readable_game_db_path(), cache)

            receipt["source"]["sha256"] = "0" * 64
            receipt_stat = receipt_path.stat()
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            os.utime(receipt_path, ns=(receipt_stat.st_atime_ns, receipt_stat.st_mtime_ns + 2_000_000_000))
            self.assertIsNone(main._readable_game_db_path())


class ConversionStatusTests(unittest.TestCase):
    def test_partial_bundle_is_reported_as_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = [root / "CAR_A_26.zip"]

            def import_car(_game_path, _media_name, output_blend, **options):
                if options.get("body_only"):
                    return subprocess.CompletedProcess([], 1, "", "body import failed")
                output_blend.write_bytes(b"blend fixture")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                patch.object(main, "game_db_status", return_value="fixture"),
                patch.object(main, "extract_car", return_value="CAR_A_26"),
                patch.object(main, "_prepare_shared_tire_model"),
                patch.object(main, "_extract_texture_map", return_value={}),
                patch.object(main, "run_blender_import", side_effect=import_car),
            ):
                for kind in ("bundle", "bundle-zip"):
                    with self.subTest(kind=kind):
                        kind_output = root / kind
                        kind_output.mkdir(exist_ok=True)
                        old_zip = kind_output / "CAR_A_26.zip"
                        if kind == "bundle-zip":
                            old_zip.write_bytes(b"old bundle")
                        summary = main.run_conversion_job(
                            selected,
                            kind=kind,
                            workspace=root / "scratch",
                            output_dir=kind_output,
                            workers=1,
                            log=lambda _message: None,
                        )
                        self.assertEqual((summary.succeeded, summary.failed), (0, 1))
                        self.assertEqual(summary.outputs, ())
                        if kind == "bundle-zip":
                            self.assertFalse(old_zip.exists(), "Failed rerun left a stale bundle zip")

if __name__ == "__main__":
    unittest.main()
