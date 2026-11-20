"""Import + render every sample file and report which formats work.

    python tools/format_matrix.py [--quality medium] [--only label1,label2]

For each sample: run the real import pipeline (cadview.pipeline.import_cad), load the
result into one ovrtx/OVStage runtime, save a PNG, and check the frame is not blank.
Writes output/format_matrix/{report.json, report.md, contact_sheet.png, <label>.png}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cadview.pipeline import ImportFailed, ImportOptions, import_cad  # noqa: E402

W, H = 640, 360

# label -> (format description, primary file relative to samples/)
CASES = [
    ("step", "STEP AP203/214", "web/step/switcharm.step"),
    ("step_assembly", "STEP assembly (OCCT as1)", "as1-oc-214.stp"),
    ("iges", "IGES", "web/iges/example.igs"),
    ("brep", "OpenCASCADE BRep", "web/brep/bspline_surface.brep"),
    ("smb", "SMLib BRep", "box_with_large_fillet.smb"),
    ("usd_breparray", "USD with BrepArray", "occt_cylinder_brep.usda"),
    ("jt", "JT", "web/jt/conrod.jt"),
    ("parasolid_x_t", "Parasolid text (.x_t)", "web/parasolid_x_t/ring.x_t"),
    ("parasolid_xmt_txt", "Parasolid text (.xmt_txt)", "web/parasolid_xmt_txt/part.xmt_txt"),
    ("parasolid_x_b", "Parasolid binary (.x_b)", "web/parasolid_x_b/JGB37-520.x_b"),
    ("acis_sat", "ACIS text (.sat)", "web/acis_sat/yaw_shaft.sat"),
    ("catia_catpart", "CATIA V5 part", "web/catia_catpart/Piston.CATPart"),
    ("catia_catproduct", "CATIA V5 product", "web/catia_catproduct/product.CATProduct"),
    ("catia_cgr", "CATIA CGR", "web/catia_cgr/wip.cgr"),
    ("catia_3dxml", "3D XML", "web/catia_3dxml/quad.3dxml"),
    ("solidworks_sldprt", "SolidWorks part", "web/solidworks_sldprt/DC_MOTOR_JGB37-520.SLDPRT"),
    ("nx_prt", "Unigraphics / NX part", "web/nx_prt/bottle.prt"),
    ("creo_prt", "Pro/E / Creo part (V18)", "web/creo_prt/rear_panel.prt"),
    ("solidedge_par", "Solid Edge part", "web/solidedge_par/Part1.par"),
    ("solidedge_psm", "Solid Edge sheet metal", "web/solidedge_psm/Sandia_FLAME.psm"),
    ("inventor_ipt", "Inventor part", "web/inventor_ipt/Wall_door.ipt"),
    ("inventor_iam", "Inventor assembly", "web/inventor_iam/Room_Full.iam"),
    ("rhino_3dm", "Rhino", "web/rhino_3dm/Rhino_Logo.3dm"),
    ("autocad_dwg", "AutoCAD DWG", "web/autocad_dwg/MOTOR_SHELL.dwg"),
    ("autocad_dxf", "AutoCAD DXF", "web/autocad_dxf/PinkEggFromLW.dxf"),
    ("revit_rfa", "Revit family", "web/revit_rfa/family.rfa"),
    ("revit_rvt", "Revit project", "web/revit_rvt/Parking_Example_Share.rvt"),
    ("ifc", "IFC", "web/ifc/AC14-FZK-Haus.ifc"),
    ("ifczip", "IFC zip (derived from the IFC sample)", "web/ifczip/AC14-FZK-Haus.ifczip"),
    ("microstation_dgn", "MicroStation DGN", "web/microstation_dgn/shaft.dgn"),
    ("stl", "STL", "web/stl/Spider_ascii.stl"),
    ("obj", "Wavefront OBJ", "web/obj/spider.obj"),
    ("fbx", "FBX", "web/fbx/spider.fbx"),
    ("collada_dae", "Collada", "web/collada_dae/duck.dae"),
    ("3ds", "3DS", "web/3ds/model.3ds"),
    ("3mf", "3MF", "web/3mf/box.3mf"),
    ("gltf_glb", "glTF binary", "web/gltf_glb/Duck.glb"),
]


def _ensure_ifczip(samples: Path) -> None:
    ifc = samples / "web/ifc/AC14-FZK-Haus.ifc"
    dst = samples / "web/ifczip/AC14-FZK-Haus.ifczip"
    if ifc.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(ifc, ifc.name)


def _nonblank(img: np.ndarray) -> float:
    """Fraction of pixels that differ clearly from the (corner) background colour."""
    rgb = img[..., :3].astype(np.int16)
    bg = np.median(np.concatenate([rgb[:8, :8].reshape(-1, 3), rgb[-8:, -8:].reshape(-1, 3)]), axis=0)
    return float((np.abs(rgb - bg).sum(axis=2) > 30).mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quality", default="medium")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    samples = ROOT / "samples"
    out = ROOT / "output" / "format_matrix"
    out.mkdir(parents=True, exist_ok=True)
    _ensure_ifczip(samples)
    only = {s for s in a.only.split(",") if s}

    # 1) imports (worker processes; no renderer yet)
    rows = []
    for label, desc, rel in CASES:
        if only and label not in only:
            continue
        src = samples / rel
        row = {"label": label, "format": desc, "file": rel, "ext": src.suffix.lower()}
        rows.append(row)
        if not src.exists():
            row.update(status="no sample", error="sample file not available")
            print(f"--  {label:20} no sample")
            continue
        t0 = time.perf_counter()
        try:
            res = import_cad(src, ImportOptions(quality=a.quality))
            info = res.info
            row.update(
                status="imported", route=res.route, usd=res.usd_path, info=info, parts=res.parts,
                exact_parts=res.exact_parts, fallback_parts=res.fallback_parts, warnings=res.warnings,
                meshes=info.get("meshes", 0), brep_arrays=info.get("brep_arrays", 0),
            )  # fmt: skip
            row["curves"] = info.get("curves", 0)
            if not info.get("bounds"):
                row.update(status="no geometry", error="import produced no visible geometry")
        except ImportFailed as exc:
            tail = [ln for ln in exc.log if ln.strip()][-3:]
            row.update(status="import failed", error=f"{exc} | " + " | ".join(tail))
        except Exception as exc:  # noqa: BLE001
            row.update(status="import failed", error=f"{type(exc).__name__}: {exc}")
        row["import_s"] = round(time.perf_counter() - t0, 2)
        print(f"{'OK' if row['status'] == 'imported' else '!!'}  {row['label']:20} {row['status']:14} "
              f"{row.get('route', ''):8} meshes={row.get('meshes', '-')} brep={row.get('brep_arrays', '-')} "
              f"{row['import_s']}s {row.get('error', '')[:160]}", flush=True)

    # 2) renders through one ovrtx + OVStage runtime
    from PIL import Image

    from cadview.camera import OrbitCamera
    from cadview.renderer_runtime import RenderRuntime, SceneSpec

    rt = RenderRuntime(W, H)
    rt.ready.wait()
    for row in rows:
        if row["status"] != "imported":
            continue
        info = row["info"]
        cam = OrbitCamera(up_axis=info.get("up_axis", "Z"), aspect=W / H)
        cam.fit(info["bounds"])
        try:
            before, _ = rt.latest_frame()
            rt.load(SceneSpec(row["usd"], info["bounds"], info.get("up_axis", "Z"), info.get("meters_per_unit", 0.01),
                              viewer_lights=info.get("lights", 0) == 0, camera_matrix=cam.matrix()))  # fmt: skip
            if not rt.scene_loaded.wait(900) or rt.load_error:
                raise RuntimeError(rt.load_error or "timed out loading into ovrtx")
            time.sleep(0.6)  # settle frames
            fid, frame = rt.latest_frame()
            if fid <= before or frame is None:
                raise RuntimeError("no frame rendered")
            png = out / f"{row['label']}.png"
            Image.fromarray(frame).save(png)
            row["png"] = png.name
            row["coverage"] = round(_nonblank(frame), 4)
            row["status"] = "rendered" if row["coverage"] > 0.002 else "blank render"
        except Exception as exc:  # noqa: BLE001
            row.update(status="render failed", error=f"{exc}")
            traceback.print_exc()
        print(f"    render {row['label']:20} {row['status']} coverage={row.get('coverage')}", flush=True)
    rt.shutdown()

    # 3) report + contact sheet
    for row in rows:
        row.pop("info", None)
    (out / "report.json").write_text(json.dumps(rows, indent=2))
    lines = ["| Format | Ext | Sample | Route | Result | Meshes | Curves | BrepArray | Notes |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        note = r.get("error") or "; ".join(r.get("warnings", []))
        lines.append(f"| {r['format']} | `{r['ext']}` | `{Path(r['file']).name}` | {r.get('route', '-')} | {r['status']} | "
                     f"{r.get('meshes', '-')} | {r.get('curves', '-')} | {r.get('brep_arrays', '-')} | {note[:160]} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")

    from PIL import ImageDraw

    tiles = [r for r in rows]
    cols, tw, th = 6, 320, 200
    sheet = Image.new("RGB", (cols * tw, ((len(tiles) + cols - 1) // cols) * (th + 22)), (24, 26, 30))
    draw = ImageDraw.Draw(sheet)
    for i, r in enumerate(tiles):
        x, y = (i % cols) * tw, (i // cols) * (th + 22)
        if r.get("png"):
            im = Image.open(out / r["png"]).convert("RGB").resize((tw - 4, th - 4))
            sheet.paste(im, (x + 2, y + 2))
        else:
            draw.rectangle([x + 2, y + 2, x + tw - 3, y + th - 3], fill=(60, 30, 30))
            draw.text((x + 10, y + th // 2 - 6), r["status"], fill=(255, 160, 160))
        ok = r["status"] == "rendered"
        draw.text((x + 6, y + th + 4), f"{r['ext']}  {r['status']}", fill=(150, 220, 80) if ok else (255, 150, 120))
    sheet.save(out / "contact_sheet.png")
    ok = sum(r["status"] == "rendered" for r in rows)
    print(f"\n{ok}/{len(rows)} rendered. Report: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
