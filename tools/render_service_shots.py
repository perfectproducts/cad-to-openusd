"""Render the format-matrix results with an external ovrtx Render Service (default http://127.0.0.1:8000).

    python tools/render_service_shots.py [--url http://127.0.0.1:8000] [--size 960x540]

The service renders from a camera that already exists in the scene. Converted files have no
camera, so for each one this writes a small "shot" layer next to the report that sublayers the
converted USD unchanged and adds a camera framed on its bounds. The service adds its own lighting;
--shot-lights also adds a headlight and dome fill to unlit models. Images land in output/format_matrix/service/<label>.png and the
job outcomes in output/format_matrix/service/results.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cadview import config  # noqa: E402
from cadview.camera import FOCAL_LENGTH, H_APERTURE, OrbitCamera  # noqa: E402

CAMERA = "/RenderShot/Camera"


def _m4(m) -> str:
    return "(" + ", ".join("(" + ", ".join(f"{v:.17g}" for v in row) + ")" for row in m) + ")"


def shot_layer(usd: str, info: dict, w: int, h: int, add_lights: bool = False) -> str:
    cam = OrbitCamera(up_axis=info.get("up_axis", "Z"), aspect=w / h)
    cam.fit(info["bounds"])
    import numpy as np

    lo, hi = np.asarray(info["bounds"][0]), np.asarray(info["bounds"][1])
    radius = max(float(np.linalg.norm(hi - lo)) * 0.5, 1e-6)
    lights = ""
    dome = ""
    if add_lights and info.get("lights", 0) == 0:
        lights = """
        def DistantLight "HeadLight"
        {
            float inputs:angle = 2
            float inputs:intensity = 1300
            float3 xformOp:rotateXYZ = (18, 22, 0)
            uniform token[] xformOpOrder = ["xformOp:rotateXYZ"]
        }"""
        dome = """
    def DomeLight "Fill"
    {
        float inputs:intensity = 450
        color3f inputs:color = (0.92, 0.95, 1.0)
    }"""
    return f"""#usda 1.0
(
    subLayers = [@{usd.replace(chr(92), '/')}@]
    upAxis = "{info.get('up_axis', 'Z')}"
    metersPerUnit = {float(info.get('meters_per_unit', 0.01))!r}
)

def Scope "RenderShot"
{{
    def Camera "Camera"
    {{
        float focalLength = {FOCAL_LENGTH}
        float horizontalAperture = {H_APERTURE}
        float verticalAperture = {H_APERTURE * h / w}
        float2 clippingRange = ({radius * 1e-3!r}, {radius * 200.0!r})
        matrix4d xformOp:transform = {_m4(cam.matrix())}
        uniform token[] xformOpOrder = ["xformOp:transform"]
{lights}
    }}
{dome}
}}
"""


def _http(url: str, data: dict | None = None, method: str | None = None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"),
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        ctype = r.headers.get("Content-Type", "")
        raw = r.read()
    return (json.loads(raw) if "json" in ctype else raw), ctype


def _info(usd: str) -> dict:
    out = subprocess.run([sys.executable, "-m", "cadview.workers.usd_compose", "info", usd],
                         capture_output=True, text=True, cwd=str(ROOT))
    return json.loads(out.stdout.strip().splitlines()[-1])


def _save_result(result, ctype: str, dest: Path) -> str:
    """Accept raw image bytes, a JSON with base64 data, or a JSON naming a file on disk."""
    if isinstance(result, bytes):
        dest.write_bytes(result)
        return "bytes"
    for key in ("image_base64", "image", "data", "png"):
        if isinstance(result.get(key), str) and len(result[key]) > 200:
            dest.write_bytes(base64.b64decode(result[key].split(",")[-1]))
            return key
    for key in ("image_path", "path", "output_path", "file", "result_path"):
        if isinstance(result.get(key), str) and Path(result[key]).exists():
            dest.write_bytes(Path(result[key]).read_bytes())
            return key
    raise RuntimeError(f"unrecognised result payload: {str(result)[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--size", default="960x540")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--shot-lights", action="store_true",
                    help="add a headlight + dome to unlit models (off: the service lights scenes itself)")
    a = ap.parse_args()
    w, h = (int(v) for v in a.size.split("x"))
    base = config.OUTPUT_DIR / "format_matrix"
    out = base / "service"
    out.mkdir(parents=True, exist_ok=True)
    rows = json.loads((base / "report.json").read_text())
    health, _ = _http(f"{a.url}/health")
    print("service health:", health.get("status"), "worker_alive:", health.get("worker_alive"))

    results = []
    for row in rows:
        rec = {"label": row["label"], "status": "skipped"}
        results.append(rec)
        usd = row.get("usd")
        if row.get("status") != "rendered" or not usd:
            rec["reason"] = f"import status: {row.get('status')}"
            continue
        info = _info(usd)
        if not info.get("bounds"):
            rec["reason"] = "no bounds"
            continue
        shot = (out / f"{row['label']}_shot.usda").resolve()  # the service needs absolute paths
        shot.write_text(shot_layer(usd, info, w, h, a.shot_lights))
        t0 = time.perf_counter()
        job, _ = _http(f"{a.url}/render", {"usd_path": str(shot), "camera_path": CAMERA, "width": w, "height": h,
                                           "force_reload": True, "scene_version": f"{row['label']}-{time.time():.0f}"})
        jid = job["job_id"]
        rec.update(job_id=jid)
        while True:
            j, _ = _http(f"{a.url}/jobs/{jid}")
            if j["status"] in ("done", "failed", "cancelled"):
                break
            if time.perf_counter() - t0 > a.timeout:
                _http(f"{a.url}/jobs/{jid}", method="DELETE")
                j = {"status": "failed", "error": "client timeout"}
                break
            time.sleep(0.5)
        rec.update(status=j["status"], error=j.get("error"), render_seconds=j.get("render_seconds"),
                   waited_seconds=j.get("waited_seconds"), wall_seconds=round(time.perf_counter() - t0, 2))
        if j["status"] == "done":
            try:
                res, ctype = _http(f"{a.url}/jobs/{jid}/result")
                rec["result_kind"] = _save_result(res, ctype, out / f"{row['label']}.png")
                rec["png"] = f"{row['label']}.png"
            except Exception as exc:  # noqa: BLE001
                rec.update(status="failed", error=f"result download: {exc}")
        print(f"{rec['status']:9} {row['label']:20} render={rec.get('render_seconds')}s wall={rec.get('wall_seconds')}s {rec.get('error') or ''}", flush=True)

    (out / "results.json").write_text(json.dumps(results, indent=2))
    done = sum(r["status"] == "done" for r in results)
    print(f"\n{done} rendered by the service; results in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
