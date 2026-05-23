"""ForzaTech Wrapper - TUI for converting Forza car models to Blender."""

import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Label, Log, SelectionList
from textual.widgets.selection_list import Selection

load_dotenv()

FORZA_PATH = Path(os.getenv("FORZA_PATH", ""))
BLENDER_PATH = Path(os.getenv("BLENDER_PATH", ""))
CARS_PATH = FORZA_PATH / "media" / "Cars"
WORKSPACE = Path(__file__).parent / "workspace"
SCRIPT_PATH = Path(__file__).parent / "ForzaTech-extraction-tools" / "scripts" / "carbin_importer.py"

# Part types we want for body-only export (exterior panels for 3D printing)
BODY_PART_TYPES = {2, 9, 34, 35, 36, 37}  # CarBody, RearWing, FrontBumper, RearBumper, Hood, SideSkirts

# Model type names to EXCLUDE in body-only mode (interior/mechanical)
EXCLUDED_MODEL_TYPES = {
    "Chassis", "Engine", "Dash", "Floor", "Seats", "CenterConsole",
    "CenterStack", "Pillar", "InteriorWindows", "InteriorLOD",
    "Tires", "ControlArm", "Gauges", "Details", "Windows",
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


def generate_blender_script(game_path: str, media_name: str, body_only: bool = False) -> str:
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
            '        if "interior" in path_resolver.resolve(model.path).lower():\n'
            '            continue\n',
        )

    return script


def run_blender_import(game_path: str, media_name: str, output_blend: Path, body_only: bool = False) -> subprocess.CompletedProcess:
    script_content = generate_blender_script(game_path, media_name, body_only=body_only)
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


class ForzaTechApp(App):
    CSS = """
    #main { height: 1fr; }
    #car-list { width: 2fr; height: 1fr; }
    #sidebar { width: 1fr; padding: 1; }
    #log-panel { height: 1fr; border: solid green; }
    #status { padding: 1; }
    Button { margin: 1 0; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("a", "select_all", "Select All"),
        ("n", "select_none", "Select None"),
    ]

    def __init__(self):
        super().__init__()
        self._log_lines: list[str] = []

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)
        self.query_one("#log-panel", Log).write_line(msg)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            car_zips = get_car_zips()
            selections = [Selection(z.stem, z, False) for z in car_zips]
            yield SelectionList[Path](*selections, id="car-list")
            with Vertical(id="sidebar"):
                yield Label(f"Cars found: {len(car_zips)}", id="status")
                yield Button("Convert (Full)", id="convert", variant="success")
                yield Button("Convert (Body Only)", id="convert-body", variant="primary")
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
        if not CARS_PATH.exists():
            self._log("[ERROR] Cars path not found!")
        if not BLENDER_PATH.exists():
            self._log("[ERROR] Blender executable not found!")
        if not SCRIPT_PATH.exists():
            self._log("[ERROR] carbin_importer.py not found!")

    def on_selection_list_selected_changed(self) -> None:
        sel_list = self.query_one("#car-list", SelectionList)
        self.query_one("#status", Label).update(f"Selected: {len(sel_list.selected)}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "convert":
            self.do_convert(body_only=False)
        elif event.button.id == "convert-body":
            self.do_convert(body_only=True)
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

    def do_convert(self, body_only: bool) -> None:
        self._convert(body_only)

    @work(thread=True)
    def _convert(self, body_only: bool) -> None:
        sel_list = self.query_one("#car-list", SelectionList)
        selected: list[Path] = list(sel_list.selected)
        mode = "Body Only" if body_only else "Full"

        if not selected:
            self.call_from_thread(self._log, "No cars selected!")
            return

        WORKSPACE.mkdir(parents=True, exist_ok=True)
        self.call_from_thread(self._log, f"\n=== Converting {len(selected)} car(s) [{mode}] ===")
        success = 0
        failed = 0

        for i, zip_path in enumerate(selected, 1):
            self.call_from_thread(self._log, f"\n[{i}/{len(selected)}] Processing {zip_path.stem}...")

            self.call_from_thread(self._log, f"  Extracting {zip_path.name}...")
            try:
                media_name = extract_car(zip_path, WORKSPACE)
            except Exception as e:
                self.call_from_thread(self._log, f"  [ERROR] Extract failed: {e}")
                failed += 1
                continue

            suffix = "_body" if body_only else ""
            output_blend = WORKSPACE / f"{media_name}{suffix}.blend"
            self.call_from_thread(self._log, f"  Running Blender import [{mode}] for {media_name}...")
            try:
                result = run_blender_import(str(WORKSPACE), media_name, output_blend, body_only=body_only)
                if result.returncode != 0:
                    self.call_from_thread(self._log, f"  [FAIL] Blender exited with code {result.returncode}")
                    if result.stderr:
                        for line in result.stderr.strip().splitlines()[-5:]:
                            self.call_from_thread(self._log, f"    {line}")
                    failed += 1
                elif output_blend.exists():
                    self.call_from_thread(self._log, f"  [OK] Saved: {output_blend}")
                    success += 1
                else:
                    self.call_from_thread(self._log, f"  [FAIL] Import errored - .blend not saved")
                    errors = [l for l in result.stdout.splitlines() if "Error" in l or "Traceback" in l]
                    if errors:
                        for line in errors[-3:]:
                            self.call_from_thread(self._log, f"    {line}")
                    elif result.stderr:
                        for line in result.stderr.strip().splitlines()[-3:]:
                            self.call_from_thread(self._log, f"    {line}")
                    failed += 1
            except subprocess.TimeoutExpired:
                self.call_from_thread(self._log, "  [TIMEOUT] Blender took too long")
                failed += 1
            except Exception as e:
                self.call_from_thread(self._log, f"  [ERROR] {e}")
                failed += 1

        self.call_from_thread(
            self._log,
            f"\nDone! {success} succeeded, {failed} failed out of {len(selected)}."
            + (f"\nOutput: {WORKSPACE}" if success > 0 else ""),
        )


def main():
    app = ForzaTechApp()
    app.run()


if __name__ == "__main__":
    main()
