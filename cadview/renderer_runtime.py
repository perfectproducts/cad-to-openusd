"""Render runtime: the single owner of the ovrtx Renderer and the OVStage Stage.

All renderer/stage work happens on one background thread. The UI only enqueues
commands (load scene, camera pose) and picks up the latest copied RGBA frame.
Write protocol per update: write @N -> wait -> advance write floor to N -> step(N).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

os.environ.setdefault("OVRTX_SKIP_USD_CHECK", "1")  # before ovrtx/ovstage import

import numpy as np  # noqa: E402
import ovrtx  # noqa: E402

ovrtx.register_schema_paths()  # before OVStage / any OpenUSD consumer initialises
import ovstage  # noqa: E402
from ovstage import AttributeSemantic, DLDataType, DLDataTypeCode, PopulationDomain, PrimMode, make_dltensor, population  # noqa: E402

from .camera import FOCAL_LENGTH, H_APERTURE  # noqa: E402

log = logging.getLogger("cadview.render")

CAMERA_PATH = "/CadViewSession/Camera"
PRODUCT_PATH = "/CadViewSession/Render/Viewport"
LDR_PATH = "/CadViewSession/Render/Vars/LdrColor"


@dataclass
class SceneSpec:
    usd_path: str
    bounds: list | None
    up_axis: str = "Z"
    meters_per_unit: float = 0.01
    viewer_lights: bool = True
    camera_matrix: np.ndarray | None = None


def _m4(m: np.ndarray) -> str:
    return "(" + ", ".join("(" + ", ".join(f"{v:.17g}" for v in row) + ")" for row in m) + ")"


def viewer_root_usda(spec: SceneSpec, width: int, height: int) -> str:
    """Session root: sublayers the (unmodified) asset and adds camera, product, lights."""
    scene = spec.usd_path.replace("\\", "/")
    v_aperture = H_APERTURE * height / width
    radius = 1.0
    if spec.bounds:
        lo, hi = np.asarray(spec.bounds[0]), np.asarray(spec.bounds[1])
        radius = max(float(np.linalg.norm(hi - lo)) * 0.5, 1e-6)
    near, far = radius * 1e-3, radius * 200.0
    cam = spec.camera_matrix if spec.camera_matrix is not None else np.eye(4)
    lights = ""
    if spec.viewer_lights:
        # Headlight parented to the camera (follows the view) + soft dome fill.
        lights = """
        def DistantLight "HeadLight"
        {
            float inputs:angle = 2
            float inputs:intensity = 1300
            float3 xformOp:rotateXYZ = (18, 22, 0)
            uniform token[] xformOpOrder = ["xformOp:rotateXYZ"]
        }"""
    dome = (
        """
    def DomeLight "Fill"
    {
        float inputs:intensity = 450
        color3f inputs:color = (0.92, 0.95, 1.0)
    }"""
        if spec.viewer_lights
        else ""
    )
    return f"""#usda 1.0
(
    subLayers = [@{scene}@]
    upAxis = "{spec.up_axis}"
    metersPerUnit = {float(spec.meters_per_unit)!r}
)

def Scope "CadViewSession"
{{
    def Camera "Camera"
    {{
        float focalLength = {FOCAL_LENGTH}
        float horizontalAperture = {H_APERTURE}
        float verticalAperture = {v_aperture}
        float2 clippingRange = ({near!r}, {far!r})
        token projection = "perspective"
        matrix4d xformOp:transform = {_m4(cam)}
        uniform token[] xformOpOrder = ["xformOp:transform"]
{lights}
    }}
{dome}
    def Scope "Render"
    {{
        def RenderProduct "Viewport"
        {{
            rel camera = <{CAMERA_PATH}>
            rel orderedVars = [<{LDR_PATH}>]
            uniform int2 resolution = ({int(width)}, {int(height)})
        }}
        def Scope "Vars"
        {{
            def RenderVar "LdrColor"
            {{
                uniform string sourceName = "LdrColor"
            }}
        }}
    }}
}}
"""


class RenderRuntime:
    """Owns renderer + stage on a worker thread; thread-safe command/frame API."""

    SETTLE_FRAMES = 6  # frames rendered after each change so denoising converges

    def __init__(self, width: int = 1600, height: int = 900):
        self.width, self.height = width, height
        self._cmds: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_id = 0
        self.status = "Starting RTX renderer..."
        self.error: str | None = None
        self.ready = threading.Event()
        self.scene_loaded = threading.Event()
        self.first_frame_seconds: float | None = None
        self.load_error: str | None = None
        self.last_step_ms = 0.0
        self._thread = threading.Thread(target=self._run, name="cadview-render", daemon=True)
        self._thread.start()

    # ---- UI-facing API (any thread) -------------------------------------------------
    def load(self, spec: SceneSpec) -> None:
        self.scene_loaded.clear()
        self._cmds.put(("load", spec))
        self._wake.set()

    def set_camera(self, matrix: np.ndarray) -> None:
        self._cmds.put(("camera", np.array(matrix, dtype=np.float64)))
        self._wake.set()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._cmds.put(("quit", None))
        self._wake.set()
        self._thread.join(timeout)

    def latest_frame(self) -> tuple[int, np.ndarray | None]:
        with self._lock:
            return self._frame_id, self._frame

    # ---- render thread ---------------------------------------------------------------
    def _run(self) -> None:
        self._renderer = None
        self._stage = None
        self._ordinal = 0
        self._pending_frames = 0
        try:
            t0 = time.perf_counter()
            self._renderer = ovrtx.Renderer(
                config=ovrtx.RendererConfig(sync_mode=True, active_cuda_gpus="0", keep_system_alive=True)
            )
            log.info("ovrtx %s renderer ready in %.1fs", self._renderer.version, time.perf_counter() - t0)
            self.status = "Renderer ready - open a CAD or USD file"
            self.ready.set()
            while True:
                camera = None
                while True:
                    try:
                        kind, arg = self._cmds.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "quit":
                        return
                    if kind == "load":
                        camera = None  # a new scene carries its own initial pose
                        self.load_error = None
                        try:
                            self._load(arg)
                        except Exception as exc:  # noqa: BLE001 - a bad scene must not kill the renderer
                            log.exception("scene load failed")
                            self.load_error = f"{type(exc).__name__}: {exc}"
                            self.status = "Load failed: " + self.load_error
                            self._drop_stage()
                            self.scene_loaded.set()
                    elif kind == "camera":
                        camera = arg  # coalesce: only the newest pose matters
                if self._stage is None:
                    self._wait()
                    continue
                if camera is not None:
                    self._write_camera(camera)
                    self._pending_frames = self.SETTLE_FRAMES
                if self._pending_frames > 0:
                    self._render_one()
                    self._pending_frames -= 1
                else:
                    self._wait()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI status bar
            log.exception("render thread failed")
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = "Renderer error: " + self.error
            self.ready.set()
        finally:
            self._teardown()

    def _wait(self) -> None:
        self._wake.wait(0.1)
        self._wake.clear()

    def _teardown(self) -> None:
        try:
            if self._renderer is not None and self._stage is not None:
                self._renderer.detach_ovstage()
            if self._stage is not None:
                self._stage.destroy()
            if self._renderer is not None:
                self._renderer.destroy()
        except Exception:  # noqa: BLE001
            log.exception("renderer teardown")
        self._stage = self._renderer = None

    def _drop_stage(self) -> None:
        if self._stage is not None:
            try:
                self._renderer.detach_ovstage()
            except Exception:  # noqa: BLE001 - stage may never have been attached
                pass
            self._stage.destroy()
            self._stage = None

    def _load(self, spec: SceneSpec) -> None:
        self.status = f"Loading {os.path.basename(spec.usd_path)} into OVStage..."
        self._drop_stage()
        t0 = time.perf_counter()
        stage = ovstage.Stage("cadview")
        self._stage = stage  # owned from here on, so a failed load still cleans it up
        self._ordinal += 1
        population.open_usd_from_string(
            stage, viewer_root_usda(spec, self.width, self.height),
            ordinal=self._ordinal, domains=PopulationDomain.RENDERING,
        )  # fmt: skip
        stage.advance_write_floor(self._ordinal).wait()
        self.status = "Preparing RTX pipelines (the first run compiles shaders and can take a few minutes)..."
        self._renderer.attach_ovstage(stage)
        log.info("populated %s in %.2fs", spec.usd_path, time.perf_counter() - t0)
        self.status = "Rendering first frame (first run compiles RTX shaders - can take minutes)..."
        t1 = time.perf_counter()
        self._render_one()
        if self.first_frame_seconds is None:
            self.first_frame_seconds = time.perf_counter() - t1
        self._pending_frames = self.SETTLE_FRAMES
        self.status = f"Loaded {os.path.basename(spec.usd_path)}"
        self.scene_loaded.set()

    def _write_camera(self, matrix: np.ndarray) -> None:
        self._ordinal += 1
        data = np.ascontiguousarray(matrix.reshape(1, 16), dtype=np.float64)
        tensor = make_dltensor(data, dtype=DLDataType(code=DLDataTypeCode.kDLFloat, bits=64, lanes=16), shape=[1])
        pd = ovstage.PathDictionary(self._stage)
        with pd:
            path_list = pd.create_path_list_from_strings([CAMERA_PATH])
            try:
                q = self._stage.query_from_path_list(path_list)
                try:
                    op = self._stage.write_attribute(
                        q, "omni:xform", self._ordinal, tensor,
                        is_array=False, semantic=AttributeSemantic.MATRIX, prim_mode=PrimMode.UPSERT,
                    )  # fmt: skip
                    op.wait()
                    if not op.ok:
                        raise RuntimeError(f"camera write failed: {op.error_message()}")
                finally:
                    self._stage.release_query(q)
            finally:
                pd.destroy_path_list(path_list)
        del data  # buffer had to outlive op.wait()
        self._stage.advance_write_floor(self._ordinal).wait()

    def _render_one(self) -> None:
        t0 = time.perf_counter()
        products = self._renderer.step(render_products={PRODUCT_PATH}, delta_time=1.0 / 60.0, ordinal=self._ordinal)
        frame = None
        for fr in products[PRODUCT_PATH].frames:
            if LDR_PATH in fr.render_vars:
                with fr.render_vars[LDR_PATH].map(device=ovrtx.Device.CPU) as rv:
                    frame = np.from_dlpack(rv).copy()  # copy inside the map context
                break
        del products  # release step outputs before the next step / renderer teardown
        self.last_step_ms = (time.perf_counter() - t0) * 1000.0
        if frame is not None:
            with self._lock:
                self._frame = np.ascontiguousarray(frame)
                self._frame_id += 1


def wait_for_frame(rt: RenderRuntime, after_id: int, timeout: float) -> tuple[int, np.ndarray | None]:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        fid, frame = rt.latest_frame()
        if fid > after_id and frame is not None:
            return fid, frame
        if rt.error:
            raise RuntimeError(rt.error)
        time.sleep(0.02)
    raise TimeoutError("no frame rendered")


