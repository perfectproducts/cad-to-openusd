"""Worker: usd-brep operations on BrepArray USD layers.

Runs in its own process: `usd_brep` bundles OpenUSD 25.11 and must not share an
interpreter with another OpenUSD build (ovrtx / ovstage / usd-convert-cad pxr).

    python -m cadview.workers.brep_tess <job.json>

job.json:
    {
      "items": [{"key": "...", "brep_usd": "in.usda", "out": "out.usda"} ...],
      "smb":   [{"key": "...", "smb": "part.smb", "brep_usd": "part.usda"} ...],   # optional
      "heal": false,
      "validate": true,
      "curve_angle_deg": 25.0, "surface_angle_deg": 25.0,
      "chord_height": 0.0, "max_edge_length": 0.0
    }

For each `smb` entry the SMLib BRep is read and exported to a BrepArray layer first.
For each item the BrepArray layer is validated (SmBrep::AssertValid), then
`usd_brep.usd.tessellate_file` writes `out`: the exact BrepArray plus a UsdGeomMesh
per BRep. Results are printed as one JSON document on stdout.
"""

from __future__ import annotations

import json
import sys
import time
import traceback


def _run(job: dict) -> dict:
    import usd_brep
    import usd_brep.usd as ub

    results: dict[str, dict] = {}

    for entry in job.get("smb", []):
        key = entry["key"]
        try:
            brep = usd_brep.read_brep_from_file(entry["smb"], rebuild_uv_trim_curves=True)
            ub.export_brep(brep, entry["brep_usd"])
            results[key] = {"smb_ok": True, "faces": brep.face_count()}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            results[key] = {"smb_ok": False, "error": f"SMLib read/export failed: {exc}"}

    tess_kwargs = dict(
        chord_height_tolerance=float(job.get("chord_height", 0.0)),
        curve_angle_tolerance_deg=float(job.get("curve_angle_deg", 25.0)),
        surface_angle_tolerance_deg=float(job.get("surface_angle_deg", 25.0)),
        max_edge_length=float(job.get("max_edge_length", 0.0)),
        heal=bool(job.get("heal", False)),
    )

    for item in job.get("items", []):
        key = item["key"]
        res = results.setdefault(key, {})
        if res.get("smb_ok") is False:
            continue
        t0 = time.perf_counter()
        try:
            if job.get("validate", True):
                checks = ub.validate_geometry_file(item["brep_usd"])
                res["valid"] = all(c.ok for c in checks)
                res["validation"] = [
                    {"prim": c.prim_path, "ok": c.ok, "failures": list(c.failures)[:5]} for c in checks if not c.ok
                ]
            kwargs = dict(tess_kwargs)
            if "chord_height" in item:  # per-part absolute chord tolerance (model units)
                kwargs["chord_height_tolerance"] = float(item["chord_height"])
            tess = ub.tessellate_file(item["brep_usd"], item["out"], **kwargs)
            res["meshes"] = sum(1 for t in tess if t.ok)
            res["mesh_failures"] = [
                {"prim": t.prim_path, "index": t.packed_index, "status": t.status_name, "message": t.message}
                for t in tess
                if not t.ok
            ]
            res["ok"] = res["meshes"] > 0
        except Exception as exc:  # noqa: BLE001
            res["ok"] = False
            res["error"] = f"{type(exc).__name__}: {exc}"
        res["seconds"] = round(time.perf_counter() - t0, 3)

    from importlib.metadata import version

    return {"usd_brep_version": version("usd-brep"), "results": results}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        job = json.load(f)
    try:
        out = _run(job)
    except Exception:  # noqa: BLE001
        print(json.dumps({"fatal": traceback.format_exc()}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
