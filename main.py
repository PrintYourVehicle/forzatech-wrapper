"""ForzaTech Wrapper - TUI for converting Forza car models to Blender."""

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
CARS_PATH = FORZA_PATH / "media" / "Cars"
WORKSPACE = Path(__file__).parent / "workspace"
SCRIPT_PATH = Path(__file__).parent / "ForzaTech-extraction-tools" / "scripts" / "carbin_importer.py"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", max(1, (os.cpu_count() or 4) // 2)))

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


def get_car_zips() -> list[Path]:
    if not CARS_PATH.exists():
        return []
    return sorted(
        [f for f in CARS_PATH.iterdir() if f.suffix == ".zip" and "_Traffic_" not in f.stem],
        key=lambda p: p.stem.upper(),
    )


def extract_car(zip_path: Path, workspace: Path) -> str:
    media_name = zip_path.stem
    car_dir = workspace / "Media" / "Cars" / media_name
    car_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(car_dir)
    return media_name


def generate_blender_script(game_path: str, media_name: str, body_only: bool = False, interior_only: bool = False) -> str:
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    safe_path = game_path.replace("\\", "/")

    script = re.sub(
        r'^game_path\s*=.*$',
        f'game_path = r"{safe_path}"',
        script, count=1, flags=re.MULTILINE,
    )
    script = re.sub(
        r'^media_name\s*=.*$',
        f'media_name = "{media_name}"',
        script, count=1, flags=re.MULTILINE,
    )
    script = re.sub(
        r'^use_db\s*=.*$',
        'use_db = False',
        script, count=1, flags=re.MULTILINE,
    )

    # Inject defaults needed when use_db=False
    script = script.replace(
        'use_db = False\n',
        'use_db = False\n'
        'TireModelName = "WET_c"\n'
        'FrontTireWidthMM = 245\n'
        'OriginalFrontTireAspect = 40\n'
        'OriginalFrontWheelDiameterIN = 18\n'
        'FrontWheelDiameterIN = OriginalFrontWheelDiameterIN\n'
        'RearTireWidthMM = 295\n'
        'OriginalRearTireAspect = 35\n'
        'OriginalRearWheelDiameterIN = 19\n'
        'RearWheelDiameterIN = OriginalRearWheelDiameterIN\n'
        'ModelWheelbase = 2.6\n'
        'ModelFrontTrackOuter = 1.6\n'
        'ModelRearTrackOuter = 1.6\n'
        'BottomCenterWheelbasePosX = 0\n'
        'BottomCenterWheelbasePosY = 0\n'
        'BottomCenterWheelbasePosZ = 0\n'
        'ModelFrontStockRideHeight = 0.1\n'
        'ModelRearStockRideHeight = 0.1\n',
        1,
    )

    # Patch: skip models whose files don't exist (e.g. tires from _library)
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

    # Patch: guard tire_models access against None (tires skipped)
    script = script.replace(
        '    scene.part_tires.tire_models[wheel_index].modelbin.set_transform(wheel_model.modelbin.transform)',
        '    if scene.part_tires.tire_models[wheel_index] is not None:\n'
        '        scene.part_tires.tire_models[wheel_index].modelbin.set_transform(wheel_model.modelbin.transform)',
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


def run_blender_import(game_path: str, media_name: str, output_blend: Path, body_only: bool = False, interior_only: bool = False) -> subprocess.CompletedProcess:
    script_content = generate_blender_script(game_path, media_name, body_only=body_only, interior_only=interior_only)
    save_path = str(output_blend).replace("\\", "/")

    # Prepend: clear default scene objects (cube, camera, light)
    preamble = (
        'import bpy\n'
        'bpy.ops.object.select_all(action="SELECT")\n'
        'bpy.ops.object.delete()\n'
    )
    script_content = script_content.replace(
        'import bpy\n', 'import bpy\n' + preamble, 1
    )

    # Append: save .blend
    script_content += f'\nbpy.ops.wm.save_as_mainfile(filepath=r"{save_path}")\n'

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


def run_blender_export_obj(game_path: str, media_name: str, output_obj: Path, body_only: bool = False, interior_only: bool = False) -> subprocess.CompletedProcess:
    script_content = generate_blender_script(game_path, media_name, body_only=body_only, interior_only=interior_only)
    obj_path = str(output_obj).replace("\\", "/")

    preamble = (
        'import bpy\n'
        'bpy.ops.object.select_all(action="SELECT")\n'
        'bpy.ops.object.delete()\n'
    )
    script_content = script_content.replace(
        'import bpy\n', 'import bpy\n' + preamble, 1
    )

    # Export as OBJ
    script_content += (
        f'\nbpy.ops.wm.obj_export(filepath=r"{obj_path}", export_selected_objects=False)\n'
    )

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
        self._car_zips: list[Path] = get_car_zips()

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)
        self.query_one("#log-panel", Log).write_line(msg)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield Input(placeholder="Search cars...", id="search")
                selections = [Selection(z.stem, z, False) for z in self._car_zips]
                yield SelectionList[Path](*selections, id="car-list")
            with Vertical(id="sidebar"):
                yield Label(f"Cars found: {len(self._car_zips)}", id="status")
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
        filtered = [z for z in self._car_zips if query in z.stem.upper()] if query else self._car_zips
        sel_list.add_options([Selection(z.stem, z, False) for z in filtered])

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
        self._convert(body_only, interior_only)

    @work(thread=True)
    def _convert(self, body_only: bool, interior_only: bool) -> None:
        sel_list = self.query_one("#car-list", SelectionList)
        selected: list[Path] = list(sel_list.selected)
        mode = "Body Only" if body_only else "Interior Only" if interior_only else "Full"

        if not selected:
            self.call_from_thread(self._log, "No cars selected!")
            return

        WORKSPACE.mkdir(parents=True, exist_ok=True)
        self.call_from_thread(self._log, f"\n=== Converting {len(selected)} car(s) [{mode}], {MAX_WORKERS} workers ===")

        success = 0
        failed = 0
        lock = __import__("threading").Lock()

        def process_car(args: tuple[int, Path]) -> None:
            nonlocal success, failed
            i, zip_path = args
            media_name = zip_path.stem

            try:
                media_name = extract_car(zip_path, WORKSPACE)
            except Exception as e:
                self.call_from_thread(self._log, f"  [{media_name}] Extract failed: {e}")
                with lock:
                    failed += 1
                return

            suffix = "_body" if body_only else "_interior" if interior_only else ""
            output_blend = WORKSPACE / f"{media_name}{suffix}.blend"
            try:
                result = run_blender_import(str(WORKSPACE), media_name, output_blend, body_only=body_only, interior_only=interior_only)
                if result.returncode == 0 and output_blend.exists():
                    self.call_from_thread(self._log, f"  [{media_name}] [OK]")
                    with lock:
                        success += 1
                else:
                    self.call_from_thread(self._log, f"  [{media_name}] [FAIL]")
                    with lock:
                        failed += 1
            except subprocess.TimeoutExpired:
                self.call_from_thread(self._log, f"  [{media_name}] [TIMEOUT]")
                with lock:
                    failed += 1
            except Exception as e:
                self.call_from_thread(self._log, f"  [{media_name}] [ERROR] {e}")
                with lock:
                    failed += 1

            car_media = WORKSPACE / "Media" / "Cars" / media_name
            if car_media.exists():
                shutil.rmtree(car_media, True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            pool.map(process_car, enumerate(selected, 1))

        media_dir = WORKSPACE / "Media"
        if media_dir.exists() and not any(media_dir.rglob("*")):
            shutil.rmtree(media_dir, True)

        self.call_from_thread(
            self._log,
            f"\nDone! {success} succeeded, {failed} failed out of {len(selected)}."
            + (f"\nOutput: {WORKSPACE}" if success > 0 else ""),
        )


    def do_export_obj(self) -> None:
        self._export_obj()

    @work(thread=True)
    def _export_obj(self) -> None:
        sel_list = self.query_one("#car-list", SelectionList)
        selected: list[Path] = list(sel_list.selected)

        if not selected:
            self.call_from_thread(self._log, "No cars selected!")
            return

        WORKSPACE.mkdir(parents=True, exist_ok=True)
        self.call_from_thread(self._log, f"\n=== Exporting OBJ: {len(selected)} car(s), {MAX_WORKERS} workers ===")

        success = 0
        failed = 0
        lock = __import__("threading").Lock()

        def process_car(args: tuple[int, Path]) -> None:
            nonlocal success, failed
            i, zip_path = args
            media_name = zip_path.stem

            try:
                media_name = extract_car(zip_path, WORKSPACE)
            except Exception as e:
                self.call_from_thread(self._log, f"  [{media_name}] Extract failed: {e}")
                with lock:
                    failed += 1
                return

            output_obj = WORKSPACE / f"{media_name}.obj"
            try:
                result = run_blender_export_obj(str(WORKSPACE), media_name, output_obj)
                if result.returncode == 0 and output_obj.exists():
                    self.call_from_thread(self._log, f"  [{media_name}] [OK]")
                    with lock:
                        success += 1
                else:
                    self.call_from_thread(self._log, f"  [{media_name}] [FAIL]")
                    with lock:
                        failed += 1
            except subprocess.TimeoutExpired:
                self.call_from_thread(self._log, f"  [{media_name}] [TIMEOUT]")
                with lock:
                    failed += 1
            except Exception as e:
                self.call_from_thread(self._log, f"  [{media_name}] [ERROR] {e}")
                with lock:
                    failed += 1

            car_media = WORKSPACE / "Media" / "Cars" / media_name
            if car_media.exists():
                shutil.rmtree(car_media, True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            pool.map(process_car, enumerate(selected, 1))

        media_dir = WORKSPACE / "Media"
        if media_dir.exists() and not any(media_dir.rglob("*")):
            shutil.rmtree(media_dir, True)

        self.call_from_thread(
            self._log,
            f"\nDone! {success} succeeded, {failed} failed out of {len(selected)}."
            + (f"\nOutput: {WORKSPACE}" if success > 0 else ""),
        )

    def do_extract_bundle(self, as_zip: bool = False) -> None:
        self._extract_bundle(as_zip)

    @work(thread=True)
    def _extract_bundle(self, as_zip: bool) -> None:
        sel_list = self.query_one("#car-list", SelectionList)
        selected: list[Path] = list(sel_list.selected)

        if not selected:
            self.call_from_thread(self._log, "No cars selected!")
            return

        WORKSPACE.mkdir(parents=True, exist_ok=True)
        self.call_from_thread(self._log, f"\n=== Extract Bundle{'(Zip)' if as_zip else ''}: {len(selected)} car(s), {MAX_WORKERS} workers ===")

        success = 0
        failed = 0
        lock = __import__("threading").Lock()

        def process_car(args: tuple[int, Path]) -> None:
            nonlocal success, failed
            i, zip_path = args
            media_name = zip_path.stem
            self.call_from_thread(self._log, f"\n[{i}/{len(selected)}] Bundling {media_name}...")

            try:
                media_name = extract_car(zip_path, WORKSPACE)
            except Exception as e:
                self.call_from_thread(self._log, f"  [{media_name}] Extract failed: {e}")
                with lock:
                    failed += 1
                return

            modes = [
                ("Full", "", False, False),
                ("Body Only", "_body", True, False),
                ("Interior Only", "_interior", False, True),
            ]

            bundle_dir = WORKSPACE / media_name
            bundle_dir.mkdir(parents=True, exist_ok=True)
            blend_files: list[Path] = []

            for mode_name, suffix, body_only, interior_only in modes:
                output_blend = bundle_dir / f"{media_name}{suffix}.blend"
                try:
                    result = run_blender_import(str(WORKSPACE), media_name, output_blend, body_only=body_only, interior_only=interior_only)
                    if result.returncode == 0 and output_blend.exists():
                        blend_files.append(output_blend)
                        self.call_from_thread(self._log, f"  [{media_name}] [OK] {mode_name}")
                    else:
                        self.call_from_thread(self._log, f"  [{media_name}] [FAIL] {mode_name}")
                except Exception as e:
                    self.call_from_thread(self._log, f"  [{media_name}] [ERROR] {mode_name}: {e}")

            if blend_files:
                if as_zip:
                    zip_path_out = WORKSPACE / f"{media_name}.zip"
                    with zipfile.ZipFile(zip_path_out, "w", zipfile.ZIP_DEFLATED) as zf:
                        for bf in blend_files:
                            zf.write(bf, bf.name)
                    shutil.rmtree(bundle_dir, True)
                    self.call_from_thread(self._log, f"  [{media_name}] [BUNDLE] {zip_path_out.name}")
                else:
                    self.call_from_thread(self._log, f"  [{media_name}] [BUNDLE] {bundle_dir.name}/")
                with lock:
                    success += 1
            else:
                self.call_from_thread(self._log, f"  [{media_name}] [FAIL] No exports succeeded")
                with lock:
                    failed += 1

            # Cleanup extracted media
            car_media = WORKSPACE / "Media" / "Cars" / media_name
            if car_media.exists():
                shutil.rmtree(car_media, True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            pool.map(process_car, enumerate(selected, 1))

        media_dir = WORKSPACE / "Media"
        if media_dir.exists() and not any(media_dir.rglob("*")):
            shutil.rmtree(media_dir, True)

        self.call_from_thread(
            self._log,
            f"\nBundle complete! {success} succeeded, {failed} failed out of {len(selected)}.",
        )


def main():
    app = ForzaTechApp()
    app.run()


if __name__ == "__main__":
    main()
