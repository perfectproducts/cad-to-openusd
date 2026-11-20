"""Worker: OpenUSD authoring and queries with the `pxr` bundled in usd-convert-cad.

Runs in its own process so this OpenUSD build never meets the ones inside
usd-brep or ovrtx/ovstage.

    python -m cadview.workers.usd_compose assemble <manifest.json> <out.usda|out.usdc>
        Writes the assembly layer: one Xform per CAD assembly node (with its
        transform), each part referenced from its per-part USD file (exact
        BrepArray + tessellated UsdGeomMesh), CAD colours as UsdPreviewSurface
        materials. Prints the `info` JSON of the result.

    python -m cadview.workers.usd_compose curve_widths <file.usd>
        Sets a visible constant width on every BasisCurves prim (file is ours, rewritten in place).

    python -m cadview.workers.usd_compose convert <in.usd*> <out.usda|out.usdc>
        Re-saves a layer as text or binary crate.

    python -m cadview.workers.usd_compose info <file.usd>
        Prints JSON: bounds, upAxis, metersPerUnit, defaultPrim, prim counts.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from usd_convert_cad._runtime import configure_runtime_paths

configure_runtime_paths()

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade  # noqa: E402


def _ident(name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name or "Node")
    if not base or base[0].isdigit():
        base = "_" + base
    out, i = base, 1
    while out in used:
        i += 1
        out = f"{base}_{i}"
    used.add(out)
    return out


def _rel(path: Path, start: Path) -> str:
    return "./" + os.path.relpath(path, start).replace("\\", "/")


def info(usd_path: str) -> dict:
    stage = Usd.Stage.Open(usd_path)
    if stage is None:
        raise RuntimeError(f"Cannot open {usd_path}")
    counts: dict[str, int] = {}
    lights = 0
    for prim in stage.Traverse():
        t = str(prim.GetTypeName())
        counts[t] = counts.get(t, 0) + 1
        if "Light" in t:
            lights += 1
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    rng = cache.ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    bounds = None
    if not rng.IsEmpty():
        bounds = [list(rng.GetMin()), list(rng.GetMax())]
    default_prim = stage.GetDefaultPrim()
    return {
        "path": str(Path(usd_path).resolve()),
        "up_axis": str(UsdGeom.GetStageUpAxis(stage)),
        "meters_per_unit": float(UsdGeom.GetStageMetersPerUnit(stage)),
        "default_prim": str(default_prim.GetPath()) if default_prim else None,
        "bounds": bounds,
        "meshes": counts.get("Mesh", 0),
        "curves": counts.get("BasisCurves", 0) + counts.get("NurbsCurves", 0),
        "brep_arrays": counts.get("BrepArray", 0),
        "lights": lights,
        "prims": sum(counts.values()),
    }


def convert(src: str, dst: str) -> dict:
    """Re-save a layer in the format implied by dst's extension (.usda text / .usdc crate)."""
    layer = Sdf.Layer.FindOrOpen(src)
    if layer is None or not layer.Export(dst):
        raise RuntimeError(f"cannot convert {src} -> {dst}")
    return {"path": dst}


def curve_widths(usd_path: str, rel_width: float = 0.002) -> dict:
    """Give BasisCurves a visible line width (converters often author none -> RTX default of 1 unit)."""
    stage = Usd.Stage.Open(usd_path)
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    rng = cache.ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    diag = (rng.GetMax() - rng.GetMin()).GetLength() if not rng.IsEmpty() else 1.0
    width = max(diag * rel_width, 1e-9)
    n = 0
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.BasisCurves):
            curves = UsdGeom.BasisCurves(prim)
            curves.CreateWidthsAttr().Set([width])
            curves.SetWidthsInterpolation(UsdGeom.Tokens.constant)
            pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("widths")  # takes precedence over `widths`
            if pv and pv.IsDefined():
                pv.Set([width])
                pv.SetInterpolation(UsdGeom.Tokens.constant)
            n += 1
    stage.GetRootLayer().Save()
    return {"curves": n, "width": width}


def assemble(manifest_path: str, out_path: str) -> dict:
    manifest = json.loads(Path(manifest_path).read_text())
    out = Path(out_path).resolve()
    out_dir = out.parent
    parts = {p["id"]: p for p in manifest["parts"]}

    stage = Usd.Stage.CreateNew(str(out))
    UsdGeom.SetStageUpAxis(stage, manifest.get("up_axis", "Z"))
    UsdGeom.SetStageMetersPerUnit(stage, float(manifest.get("meters_per_unit", 0.001)))

    root_name = _ident(Path(manifest["source"]).stem, set())
    root = UsdGeom.Xform.Define(stage, f"/{root_name}")
    stage.SetDefaultPrim(root.GetPrim())
    root.GetPrim().SetCustomDataByKey("cadview:source", manifest["source"])
    root.GetPrim().SetCustomDataByKey("cadview:pipeline", manifest.get("pipeline", ""))

    looks = UsdGeom.Scope.Define(stage, root.GetPath().AppendChild("Looks"))
    materials: dict[tuple, UsdShade.Material] = {}
    used_mat_names: set[str] = set()

    def material_for(rgb) -> UsdShade.Material:
        key = tuple(round(c, 4) for c in rgb)
        if key in materials:
            return materials[key]
        name = _ident("mat_%02x%02x%02x" % tuple(int(255 * c) for c in key), used_mat_names)
        mat = UsdShade.Material.Define(stage, looks.GetPath().AppendChild(name))
        shader = UsdShade.Shader.Define(stage, mat.GetPath().AppendChild("PreviewSurface"))
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*key))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.45)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        materials[key] = mat
        return mat

    default_rgb = (0.72, 0.74, 0.78)

    def emit(node: dict, parent: Sdf.Path, used: set[str], inherited_rgb) -> None:
        path = parent.AppendChild(_ident(node.get("name") or "Node", used))
        xf = UsdGeom.Xform.Define(stage, path)
        prim = xf.GetPrim()
        if node.get("name"):
            prim.SetDisplayName(node["name"])
        if node.get("matrix"):
            m = node["matrix"]
            xf.AddTransformOp().Set(Gf.Matrix4d(*[m[i * 4:(i + 1) * 4] for i in range(4)]))
        rgb = node.get("color") or inherited_rgb
        if "part" in node:
            part = parts[node["part"]]
            rgb = node.get("color") or part.get("color") or inherited_rgb or default_rgb
            usd_file = part.get("usd")
            if usd_file:
                prim.GetReferences().AddReference(_rel(out_dir / usd_file, out_dir), Sdf.Path("/World"))
            prim.SetCustomDataByKey("cadview:part", part["id"])
            prim.SetCustomDataByKey("cadview:geometry", part.get("geometry", "missing"))
            if part.get("brep_usd"):
                prim.SetCustomDataByKey("cadview:exactBrep", _rel(out_dir / part["brep_usd"], out_dir))
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(material_for(rgb))
        for child in node.get("children", []):
            emit(child, path, set(), rgb)

    used_top: set[str] = {"Looks"}
    for node in manifest["roots"]:
        emit(node, root.GetPath(), used_top, None)

    stage.GetRootLayer().Save()
    return info(str(out))


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "assemble":
            print(json.dumps(assemble(argv[1], argv[2])))
            return 0
        if len(argv) == 3 and argv[0] == "convert":
            print(json.dumps(convert(argv[1], argv[2])))
            return 0
        if len(argv) == 2 and argv[0] == "curve_widths":
            print(json.dumps(curve_widths(argv[1])))
            return 0
        if len(argv) == 2 and argv[0] == "info":
            print(json.dumps(info(argv[1])))
            return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
