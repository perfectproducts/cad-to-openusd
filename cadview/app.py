"""CAD -> OpenUSD viewer: ovui window + ovrtx viewport + import pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import config
from .camera import OrbitCamera
from .pipeline import ImportFailed, ImportOptions, ImportResult, import_cad
from .renderer_runtime import RenderRuntime, SceneSpec

import omni.ui as ui  # noqa: E402  (after ovrtx: renderer_runtime sets up its runtime first)

log = logging.getLogger("cadview")

RENDER_W, RENDER_H = 1600, 900
QUALITIES = list(config.QUALITY)
ROUTES = [("auto", "Auto (exact BRep when possible)"), ("mesh", "Mesh only (usd-convert-cad)")]

C_BG = ui.color("#1b1d21")
C_PANEL = ui.color("#24272c")
C_HEADER = ui.color("#111214")
C_TEXT = ui.color("#d8dbe0")
C_DIM = ui.color("#8a9099")
C_ACCENT = ui.color("#76b900")
C_WARN = ui.color("#e0b040")
C_ERR = ui.color("#ff6b6b")

STYLE = {
    "Window": {"background_color": C_BG, "padding": 0, "margin": 0},
    "Label": {"color": C_TEXT, "font_size": 14},
    "Label::dim": {"color": C_DIM, "font_size": 13},
    "Label::title": {"color": C_ACCENT, "font_size": 17},
    "Label::section": {"color": C_ACCENT, "font_size": 14},
    "Label::warn": {"color": C_WARN, "font_size": 13},
    "Label::err": {"color": C_ERR, "font_size": 13},
    "Label::hud": {"color": ui.color(0.85, 0.87, 0.9, 0.75), "font_size": 13},
    "Button": {"background_color": ui.color("#33373e"), "border_radius": 3, "padding": 4},
    "Button:hovered": {"background_color": ui.color("#454a53")},
    "Button::primary": {"background_color": ui.color("#4f7d00")},
    "Button::primary:hovered": {"background_color": C_ACCENT},
    "Button.Label": {"color": C_TEXT},
    "Button::seg": {"background_color": ui.color("#2d3036"), "border_radius": 0, "padding": 2},
    "Button::seg:hovered": {"background_color": ui.color("#3b3f46")},
    "Button::seg:checked": {"background_color": ui.color("#4f7d00")},
    "Field": {"background_color": ui.color("#2d3036"), "color": C_TEXT, "border_radius": 3},
    "ComboBox": {"background_color": ui.color("#2d3036"), "color": C_TEXT, "border_radius": 3},
    "Rectangle::header": {"background_color": C_HEADER},
    "Rectangle::panel": {"background_color": C_PANEL},
    "Rectangle::viewport": {"background_color": ui.color("#15171a")},
    "Rectangle::overlay": {"background_color": ui.color(0.0, 0.0, 0.0, 0.55), "border_radius": 6},
    "ProgressBar": {"color": C_ACCENT, "background_color": ui.color("#2d3036"), "secondary_color": 0x00000000, "border_radius": 3},
}


def _browse_dialog(initial_dir: str) -> str | None:
    """Native file dialog in a child process (keeps Tk out of the ovui/ovrtx process)."""
    exts = sorted(config.SUPPORTED_EXTENSIONS)
    script = (
        "import sys, tkinter as tk, tkinter.filedialog as fd\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        f"p = fd.askopenfilename(initialdir={initial_dir!r}, title='Import CAD file',"
        f" filetypes=[('CAD / USD', {' '.join('*' + e for e in exts)!r}), ('All files', '*.*')])\n"
        "print(p or '')\n"
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, creationflags=flags)
    path = out.stdout.strip()
    return path or None


def image_content_rect(widget_w, widget_h, image_w, image_h):
    image_aspect = image_w / max(1.0, float(image_h))
    widget_aspect = widget_w / max(1.0, float(widget_h))
    if widget_aspect > image_aspect:
        draw_h = float(widget_h)
        draw_w = draw_h * image_aspect
        return (widget_w - draw_w) * 0.5, 0.0, draw_w, draw_h
    draw_w = float(widget_w)
    return 0.0, (widget_h - draw_w / image_aspect) * 0.5, draw_w, draw_w / image_aspect


class CadViewApp:
    def __init__(self, initial_file: str | None = None):
        self.runtime = RenderRuntime(RENDER_W, RENDER_H)
        self.camera = OrbitCamera(aspect=RENDER_W / RENDER_H)
        self.result: ImportResult | None = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._importing = False
        self._shown_frame = -1
        self._drag_button: int | None = None
        self._last_xy = (0.0, 0.0)
        self._log_lines: list[str] = []
        self._initial_file = initial_file

        self.path_model = ui.SimpleStringModel(initial_file or str(config.ROOT / "samples" / "demo_assembly.step"))
        self.heal_model = ui.SimpleBoolModel(False)
        self.usd_format = "usdc"  # export format toggle: usdc (binary, default) | usda (text)
        self._format_buttons: dict[str, ui.Button] = {}
        self.progress_model = ui.SimpleFloatModel(0.0)
        self.provider = ui.ByteImageProvider()
        blank = np.zeros((RENDER_H, RENDER_W, 4), dtype=np.uint8)
        blank[..., :3] = (27, 29, 33)
        blank[..., 3] = 255
        self.provider.set_data_array(blank, [RENDER_W, RENDER_H])

        ui.style.default = STYLE
        self.window = ui.Window(
            "CAD to OpenUSD",
            width=1500,
            height=900,
            fill_app_window=True,
            flags=ui.WINDOW_FLAGS_NO_TITLE_BAR | ui.WINDOW_FLAGS_NO_SCROLLBAR | ui.WINDOW_FLAGS_NO_SCROLL_WITH_MOUSE,
            raster_policy=ui.RasterPolicy.NEVER,
        )
        with self.window.frame:
            self._build()

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        with ui.VStack(spacing=0):
            self._build_header()
            with ui.HStack(spacing=0):
                self._build_viewport()
                self._build_sidebar()
            self._build_status_bar()

    def _build_header(self) -> None:
        with ui.ZStack(height=ui.Pixel(52)):
            ui.Rectangle(name="header")
            with ui.HStack(spacing=8):
                ui.Spacer(width=ui.Pixel(12))
                with ui.VStack(width=ui.Pixel(150)):
                    ui.Spacer()
                    ui.Label("CAD > OpenUSD", name="title", height=ui.Pixel(20))
                    ui.Label("usd-brep | ovrtx | ovui", name="dim", height=ui.Pixel(16))
                    ui.Spacer()
                with ui.VStack():
                    ui.Spacer()
                    ui.StringField(model=self.path_model, height=ui.Pixel(28))
                    ui.Spacer()
                with ui.VStack(width=ui.Pixel(80)):
                    ui.Spacer()
                    ui.Button("Browse...", height=ui.Pixel(28), clicked_fn=self._on_browse)
                    ui.Spacer()
                with ui.VStack(width=ui.Pixel(90)):
                    ui.Spacer()
                    self.import_btn = ui.Button("IMPORT", name="primary", height=ui.Pixel(28), clicked_fn=self._on_import)
                    ui.Spacer()
                ui.Label("Export", name="dim", width=ui.Pixel(40))
                with ui.VStack(width=ui.Pixel(104)):
                    ui.Spacer()
                    with ui.HStack(height=ui.Pixel(26), spacing=1):
                        for fmt, tip in (("usdc", "Binary crate files (smaller, faster to load)"),
                                         ("usda", "Text files (human-readable, diff-friendly)")):
                            self._format_buttons[fmt] = ui.Button(
                                fmt.upper(), name="seg", checked=(fmt == self.usd_format), tooltip=tip,
                                clicked_fn=lambda f=fmt: self._set_format(f),
                            )
                    ui.Spacer()
                ui.Label("Quality", name="dim", width=ui.Pixel(46))
                with ui.VStack(width=ui.Pixel(90)):
                    ui.Spacer()
                    self.quality_combo = ui.ComboBox(QUALITIES.index("medium"), *QUALITIES, height=ui.Pixel(26))
                    ui.Spacer()
                with ui.VStack(width=ui.Pixel(220)):
                    ui.Spacer()
                    self.route_combo = ui.ComboBox(0, *[r[1] for r in ROUTES], height=ui.Pixel(26))
                    ui.Spacer()
                with ui.VStack(width=ui.Pixel(20)):
                    ui.Spacer()
                    ui.CheckBox(model=self.heal_model, height=ui.Pixel(20))
                    ui.Spacer()
                ui.Label("Heal BReps", width=ui.Pixel(72), tooltip="usd-brep healing before tessellation")
                with ui.VStack(width=ui.Pixel(60)):
                    ui.Spacer()
                    ui.Button("Fit", height=ui.Pixel(28), clicked_fn=self._on_fit, tooltip="Frame the model (or double-click the viewport)")
                    ui.Spacer()
                ui.Spacer(width=ui.Pixel(10))

    def _build_viewport(self) -> None:
        with ui.ZStack():
            ui.Rectangle(name="viewport")
            self.image = ui.ImageWithProvider(self.provider, fill_policy=ui.IwpFillPolicy.IWP_PRESERVE_ASPECT_FIT)
            # Transparent hit surface on top of the image receives all viewport input.
            self.hit = ui.Rectangle(style={"background_color": 0x00000000})
            self.hit.opaque_for_mouse_events = True
            self.hit.set_mouse_pressed_fn(self._guard(self._on_press))
            self.hit.set_mouse_released_fn(self._guard(self._on_release))
            self.hit.set_mouse_moved_fn(self._guard(self._on_move))
            self.hit.set_mouse_wheel_fn(self._guard(self._on_wheel))
            self.hit.set_mouse_double_clicked_fn(self._guard(lambda *a: self._on_fit()))
            with ui.VStack():
                ui.Spacer(height=ui.Pixel(8))
                with ui.HStack(height=ui.Pixel(18)):
                    ui.Spacer(width=ui.Pixel(10))
                    self.hud = ui.Label("", name="hud")
                ui.Spacer()
                with ui.HStack(height=ui.Pixel(18)):
                    ui.Spacer(width=ui.Pixel(10))
                    ui.Label("LMB orbit | MMB pan | RMB / wheel zoom | double-click fit", name="hud")
                ui.Spacer(height=ui.Pixel(8))
            # Busy overlay (import / first frame).
            self.overlay = ui.VStack(visible=False)
            with self.overlay:
                ui.Spacer()
                with ui.HStack(height=ui.Pixel(86)):
                    ui.Spacer()
                    with ui.ZStack(width=ui.Pixel(520)):
                        ui.Rectangle(name="overlay")
                        with ui.VStack(spacing=8):
                            ui.Spacer(height=ui.Pixel(10))
                            with ui.HStack():
                                ui.Spacer(width=ui.Pixel(16))
                                self.overlay_label = ui.Label("", elided_text=True, height=ui.Pixel(20))
                                ui.Spacer(width=ui.Pixel(16))
                            with ui.HStack(height=ui.Pixel(14)):
                                ui.Spacer(width=ui.Pixel(16))
                                ui.ProgressBar(model=self.progress_model, height=ui.Pixel(14))
                                ui.Spacer(width=ui.Pixel(16))
                            ui.Spacer()
                    ui.Spacer()
                ui.Spacer()

    def _build_sidebar(self) -> None:
        with ui.ZStack(width=ui.Pixel(340)):
            ui.Rectangle(name="panel")
            with ui.ScrollingFrame(horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF):
                with ui.HStack():
                    ui.Spacer(width=ui.Pixel(14))
                    with ui.VStack(spacing=6, height=0):
                        ui.Spacer(height=ui.Pixel(10))
                        ui.Label("MODEL", name="section", height=ui.Pixel(20))
                        self.info_label = ui.Label("No model loaded.", word_wrap=True, height=0)
                        ui.Spacer(height=ui.Pixel(6))
                        ui.Label("OPENUSD OUTPUT", name="section", height=ui.Pixel(20))
                        self.output_label = ui.Label("-", name="dim", word_wrap=True, height=0)
                        with ui.HStack(height=ui.Pixel(26), spacing=6):
                            ui.Button("Open folder", clicked_fn=self._on_open_folder)
                            ui.Button("Reload", clicked_fn=self._on_reload)
                        ui.Spacer(height=ui.Pixel(6))
                        ui.Label("WARNINGS", name="section", height=ui.Pixel(20))
                        self.warn_label = ui.Label("None", name="dim", word_wrap=True, height=0)
                        ui.Spacer(height=ui.Pixel(6))
                        ui.Label("PIPELINE LOG", name="section", height=ui.Pixel(20))
                        self.log_label = ui.Label("", name="dim", word_wrap=True, height=0)
                        ui.Spacer(height=ui.Pixel(10))
                    ui.Spacer(width=ui.Pixel(10))

    def _build_status_bar(self) -> None:
        with ui.ZStack(height=ui.Pixel(26)):
            ui.Rectangle(name="header")
            with ui.HStack():
                ui.Spacer(width=ui.Pixel(12))
                self.status_label = ui.Label("", name="dim", elided_text=True)
                self.gpu_label = ui.Label("", name="dim", width=ui.Pixel(320), alignment=ui.Alignment.RIGHT_CENTER)
                ui.Spacer(width=ui.Pixel(12))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _guard(fn):
        def wrapped(*args):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - never let a callback kill the UI loop
                log.exception("UI callback failed")

        return wrapped

    def _log(self, line: str) -> None:
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-60:]
        self.log_label.text = "\n".join(self._log_lines)

    def _render_scale(self) -> float:
        """Render pixels per screen pixel (letterboxed image)."""
        w = float(self.hit.computed_width or 1.0)
        h = float(self.hit.computed_height or 1.0)
        _, _, _, draw_h = image_content_rect(w, h, RENDER_W, RENDER_H)
        return RENDER_H / max(1.0, draw_h)

    def _push_camera(self) -> None:
        self.runtime.set_camera(self.camera.matrix())

    # ---------------------------------------------------------------- input
    def _on_press(self, x, y, button, modifiers):
        self._drag_button = button
        self._last_xy = (x, y)

    def _on_release(self, x, y, button, modifiers):
        self._drag_button = None

    def _on_move(self, x, y, modifiers, *rest):
        if self._drag_button is None:
            return
        s = self._render_scale()
        dx, dy = (x - self._last_xy[0]) * s, (y - self._last_xy[1]) * s
        self._last_xy = (x, y)
        if dx == 0 and dy == 0:
            return
        if self._drag_button == 0:  # ovui: 0=left, 1=right, 2=middle
            self.camera.orbit(dx, dy)
        elif self._drag_button == 2:
            self.camera.pan(dx, dy, RENDER_H)
        elif self._drag_button == 1:
            self.camera.dolly(1.0 + dy * 0.005)
        self._push_camera()

    def _on_wheel(self, dx, dy, *rest):
        if dy:
            self.camera.dolly(0.88 if dy > 0 else 1.0 / 0.88)
            self._push_camera()

    # ---------------------------------------------------------------- actions
    def _on_fit(self):
        if self.result and self.result.info.get("bounds"):
            self.camera.fit(self.result.info["bounds"], reset_angles=False)
            self._push_camera()

    def _on_browse(self):
        start = str(Path(self.path_model.as_string).parent) if self.path_model.as_string else str(config.ROOT)

        def worker():
            path = _browse_dialog(start)
            if path:
                self._ui_queue.put(("browse", path))

        threading.Thread(target=worker, daemon=True).start()

    def _set_format(self, fmt: str) -> None:
        self.usd_format = fmt
        for name, button in self._format_buttons.items():
            button.checked = name == fmt

    def _on_open_folder(self):
        if self.result:
            os.startfile(self.result.out_dir)  # noqa: S606 - local desktop tool

    def _on_reload(self):
        if self.result:
            self._load_result(self.result, refit=False)

    def _on_import(self):
        if self._importing:
            return
        path = self.path_model.as_string.strip().strip('"')
        if not path:
            return
        options = ImportOptions(
            quality=QUALITIES[self.quality_combo.model.get_item_value_model().as_int],
            heal=self.heal_model.as_bool,
            route=ROUTES[self.route_combo.model.get_item_value_model().as_int][0],
            usd_format=self.usd_format,
        )
        self._importing = True
        self._log_lines = []
        self.warn_label.text = "None"
        self.overlay.visible = True
        self.progress_model.set_value(0.0)
        self.overlay_label.text = f"Importing {Path(path).name}..."

        def progress(msg: str, frac: float) -> None:
            self._ui_queue.put(("progress", (msg, frac)))

        def worker():
            try:
                res = import_cad(path, options, progress)
                self._ui_queue.put(("imported", res))
            except ImportFailed as exc:
                self._ui_queue.put(("failed", (str(exc), exc.log)))
            except Exception as exc:  # noqa: BLE001
                log.exception("import failed")
                self._ui_queue.put(("failed", (f"{type(exc).__name__}: {exc}", [])))

        threading.Thread(target=worker, name="cadview-import", daemon=True).start()

    def _load_result(self, res: ImportResult, refit: bool = True) -> None:
        info = res.info
        self.camera.up_axis = info.get("up_axis", "Z")
        if refit and info.get("bounds"):
            self.camera.fit(info["bounds"])
        self.runtime.load(
            SceneSpec(
                usd_path=res.usd_path,
                bounds=info.get("bounds"),
                up_axis=info.get("up_axis", "Z"),
                meters_per_unit=info.get("meters_per_unit", 0.01),
                viewer_lights=info.get("lights", 0) == 0,
                camera_matrix=self.camera.matrix(),
            )
        )
        self.overlay.visible = True
        self.overlay_label.text = "Loading into ovrtx..."
        self.progress_model.set_value(1.0)

    def _show_result(self, res: ImportResult) -> None:
        info = res.info
        b = info.get("bounds")
        mpu = info.get("meters_per_unit", 1.0)
        size = ""
        if b:
            unit = {0.001: "mm", 0.01: "cm", 1.0: "m"}.get(round(mpu, 6), f"x {mpu:g} m")
            ext = [b[1][i] - b[0][i] for i in range(3)]
            size = f"{ext[0]:.4g} x {ext[1]:.4g} x {ext[2]:.4g} {unit}"
        route_desc = {
            "brep": "Exact BRep: OpenCASCADE > occt_to_usd > usd-brep",
            "smlib": "Exact BRep: SMLib > usd-brep",
            "usd+brep": "USD BrepArray tessellated by usd-brep",
            "usd": "OpenUSD (viewed as-is)",
            "hoops": "Mesh: usd-convert-cad (HOOPS)",
        }.get(res.route, res.route)
        lines = [
            f"Source:  {Path(res.source).name}",
            f"Route:   {route_desc}",
        ]
        if res.parts:
            lines.append(f"Parts:   {res.parts}  (exact {res.exact_parts}, fallback {res.fallback_parts})")
        lines += [
            f"BrepArray prims: {info.get('brep_arrays', 0)}",
            f"Meshes:  {info.get('meshes', 0)}   Prims: {info.get('prims', 0)}",
            f"Size:    {size}" if size else "Size:    -",
            f"Units:   metersPerUnit {mpu:g}, upAxis {info.get('up_axis')}",
            f"Import:  {res.seconds:.1f} s",
        ]
        self.info_label.text = "\n".join(lines)
        self.output_label.text = res.usd_path
        self.warn_label.text = "\n".join(res.warnings) if res.warnings else "None"
        self.warn_label.name = "warn" if res.warnings else "dim"

    # ---------------------------------------------------------------- loop
    def tick(self) -> None:
        while True:
            try:
                kind, arg = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "browse":
                self.path_model.set_value(arg)
            elif kind == "progress":
                msg, frac = arg
                self.progress_model.set_value(float(frac))
                self.overlay_label.text = msg
                self._log(msg)
            elif kind == "imported":
                self._importing = False
                self.result = arg
                self._show_result(arg)
                for w in arg.warnings:
                    self._log("WARNING " + w)
                self._load_result(arg)
            elif kind == "failed":
                self._importing = False
                msg, lines = arg
                self.overlay.visible = False
                for line in lines:
                    self._log(line)
                self._log("FAILED: " + msg)
                self.warn_label.text = msg
                self.warn_label.name = "err"

        rt = self.runtime
        fid, frame = rt.latest_frame()
        if fid != self._shown_frame and frame is not None:
            self.provider.set_data_array(frame, [frame.shape[1], frame.shape[0]])
            self._shown_frame = fid
        if self.overlay.visible and not self._importing and rt.scene_loaded.is_set():
            self.overlay.visible = False
        if self.overlay.visible and not self._importing and not rt.scene_loaded.is_set():
            self.overlay_label.text = rt.status
        self.status_label.text = rt.status if not rt.error else "ERROR: " + rt.error
        self.gpu_label.text = f"ovrtx step {rt.last_step_ms:5.1f} ms | {RENDER_W}x{RENDER_H}" if rt.last_step_ms else ""
        self.hud.text = Path(self.result.source).name if self.result else ""

        if self._initial_file and rt.ready.is_set():
            self._initial_file = None
            self._on_import()

    inspector = None

    async def update_loop(self) -> None:
        import omni.ui._ui as ui_native

        while True:
            self.tick()
            if self.inspector is not None:
                self.inspector.drain_pending(ui_native, application=self)
            await ui.next_frame()

    def shutdown(self) -> None:
        self.runtime.shutdown()
        self.window.destroy()


async def _run_until_closed(update, presentation):
    tasks = [asyncio.create_task(update), asyncio.create_task(presentation)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _maybe_enable_inspector():
    """CADVIEW_INSPECT=1: start ovui's inspector (screenshots / input automation for QA)."""
    if os.environ.get("CADVIEW_INSPECT") != "1":
        return None
    import ovui_agent_skills

    root = Path(ovui_agent_skills.__file__).parent / "skills" / "omniverse-ui-inspector"
    sys.path.insert(0, str(root))
    import ovuiinspect  # starts a localhost FastAPI server on :9910

    return ovuiinspect


def run(initial_file: str | None = None) -> None:
    ui.init("CAD to OpenUSD - usd-brep + ovrtx", width=1500, height=900, max_fps=60)
    app = CadViewApp(initial_file)
    app.inspector = _maybe_enable_inspector()
    if app.inspector is not None:
        app.inspector.attach_application(app)
    try:
        asyncio.run(_run_until_closed(app.update_loop(), ui.run_async()))
    finally:
        app.shutdown()
        ui.shutdown()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if os.environ.get("CADVIEW_DUMP_STACKS"):  # debugging aid: periodic all-thread stack dumps
        import faulthandler

        faulthandler.dump_traceback_later(float(os.environ["CADVIEW_DUMP_STACKS"]), repeat=True)
    argv = sys.argv[1:] if argv is None else argv
    run(argv[0] if argv else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
