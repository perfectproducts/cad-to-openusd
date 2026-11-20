"""Headless: import a CAD/USD file and save an ovrtx-rendered PNG.

    python -m cadview.snapshot <file> <out.png> [--quality medium] [--heal] [--size 1280x720]
"""

from __future__ import annotations

import argparse
import sys
import time

from .pipeline import ImportFailed, ImportOptions, import_cad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cadview.snapshot")
    ap.add_argument("file")
    ap.add_argument("png")
    ap.add_argument("--quality", default="medium")
    ap.add_argument("--heal", action="store_true")
    ap.add_argument("--route", default="auto")
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--format", choices=["usdc", "usda"], default="usdc")
    a = ap.parse_args(argv)
    w, h = (int(v) for v in a.size.lower().split("x"))

    try:
        res = import_cad(a.file, ImportOptions(a.quality, a.heal, a.route, a.format), lambda m, f: print(f"[{f:4.0%}] {m}", flush=True))
    except ImportFailed as exc:
        print("\n".join(exc.log), file=sys.stderr)
        return 1

    # Import ovrtx only after the import pipeline (its workers run in other processes anyway).
    from PIL import Image

    from .camera import OrbitCamera
    from .renderer_runtime import RenderRuntime, SceneSpec, wait_for_frame

    info = res.info
    cam = OrbitCamera(up_axis=info.get("up_axis", "Z"), aspect=w / h)
    if info.get("bounds"):
        cam.fit(info["bounds"])
    rt = RenderRuntime(w, h)
    rt.ready.wait()
    if rt.error:
        print(rt.error, file=sys.stderr)
        return 1
    t0 = time.perf_counter()
    rt.load(
        SceneSpec(res.usd_path, info.get("bounds"), info.get("up_axis", "Z"), info.get("meters_per_unit", 0.01),
                  viewer_lights=info.get("lights", 0) == 0, camera_matrix=cam.matrix())
    )  # fmt: skip
    rt.scene_loaded.wait(900)
    time.sleep(0.5)  # let the settle frames run
    _, frame = wait_for_frame(rt, 0, 900)
    Image.fromarray(frame).save(a.png)
    print(f"saved {a.png} ({frame.shape[1]}x{frame.shape[0]}), load+render {time.perf_counter() - t0:.1f}s, step {rt.last_step_ms:.1f} ms")
    rt.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
