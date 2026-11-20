"""Download public sample files (one per format) into samples/web/<format>/.

    python tools/fetch_web_samples.py

Sources are public GitHub repositories / vendor test files (see SOURCES). Each entry may
list several candidate URLs; the first that downloads (and is non-trivial) wins.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

RAW = "https://raw.githubusercontent.com"
NIST = f"{RAW}/MeshOS-AI/engineering-design-models/master"   # NIST Engineering Design Model Repository mirror
ASSIMP = f"{RAW}/assimp/assimp/master/test/models"

# label -> list of (candidate URLs, filename)   all files of one label land in samples/web/<label>/
SOURCES: dict[str, list[tuple[list[str], str]]] = {
    "step":       [([f"{NIST}/models/Allied-Signal/switcharm/switcharm.step"], "switcharm.step")],
    "iges":       [([f"{NIST}/models/CMU/scanner-2/partner_3.igs"], "partner_3.igs")],
    "brep":       [([f"{RAW}/NVIDIA-Omniverse/usd-brep/main/TestFiles/occt_breps/bspline_surface.brep"], "bspline_surface.brep")],
    "jt":         [([f"{ASSIMP}/JT/conrod.jt"], "conrod.jt")],
    "parasolid_x_t": [([f"{NIST}/models/Bentley/ring.x_t"], "ring.x_t")],
    "parasolid_xmt_txt": [([f"{NIST}/models/ASME97/HAN/PART1/part/part.xmt_txt"], "part.xmt_txt")],
    "parasolid_x_b": [([f"{RAW}/thedch/indoor-autonomous-system-cad/master/Motor/JGB37-520.x_b"], "JGB37-520.x_b")],
    "acis_sat":   [([f"{NIST}/models/CMU/seeker/seeker/yaw_shaft.hh.sat", f"{NIST}/models/CMU/seeker/seeker/switch_shaft.hh.sat"], "yaw_shaft.sat")],
    "acis_sab":   [([f"{RAW}/monozukuri-ai/cq-acis/main/tests/fixtures/cube.sab", f"{RAW}/monozukuri-ai/cq-acis/main/tests/data/cube.sab",
                     f"{RAW}/monozukuri-ai/cq-acis/main/examples/model.sab"], "model.sab")],
    "catia_catpart": [([f"{RAW}/LordAZUZ/CATIA-V5-Projects/main/Piston.CATPart"], "Piston.CATPart")],
    "catia_catproduct": [
        ([f"{RAW}/RobertoBochet/catia-project/master/product.CATProduct"], "product.CATProduct"),
        ([f"{RAW}/RobertoBochet/catia-project/master/raw.CATPart"], "raw.CATPart"),
        ([f"{RAW}/RobertoBochet/catia-project/master/clamp_fixed.CATPart"], "clamp_fixed.CATPart"),
        ([f"{RAW}/RobertoBochet/catia-project/master/clamp_screw.CATPart"], "clamp_screw.CATPart"),
        ([f"{RAW}/RobertoBochet/catia-project/master/part.CATPart"], "part.CATPart"),
        ([f"{RAW}/RobertoBochet/catia-project/master/tombstone.CATPart"], "tombstone.CATPart"),
    ],
    "catia_cgr":  [([f"{RAW}/RobertoBochet/catia-project/master/wip.cgr"], "wip.cgr")],
    "catia_3dxml": [([f"{RAW}/i2e-haw-hamburg/cad-in-unity/master/3D XML Example/quad.3dxml"], "quad.3dxml")],
    "solidworks_sldprt": [([f"{RAW}/thedch/indoor-autonomous-system-cad/master/Motor/DC MOTOR, JGB37-520.SLDPRT"], "DC_MOTOR_JGB37-520.SLDPRT")],
    "nx_prt":     [([f"{NIST}/models/Unigraphics/bottle.prt"], "bottle.prt")],
    "creo_prt":   [([f"{NIST}/models/PTC/V18/import18/rear_panel.prt"], "rear_panel.prt")],
    "solidedge_par": [([f"{RAW}/rasmusIT24/SolidEdge-T9/main/Part1.par"], "Part1.par")],
    "solidedge_psm": [([f"{RAW}/firemodels/cad/master/Case_Studies/Sandia_FLAME_Facility/Sandia_FLAME.psm"], "Sandia_FLAME.psm")],
    "inventor_ipt": [([f"{RAW}/firemodels/cad/master/FDS_Validation_Guide_Drawings/Steckler_Compartment/Wall_door.ipt"], "Wall_door.ipt")],
    "inventor_iam": [
        ([f"{RAW}/firemodels/cad/master/FDS_Validation_Guide_Drawings/Steckler_Compartment/Room_Full.iam"], "Room_Full.iam"),
        ([f"{RAW}/firemodels/cad/master/FDS_Validation_Guide_Drawings/Steckler_Compartment/floor.ipt"], "floor.ipt"),
        ([f"{RAW}/firemodels/cad/master/FDS_Validation_Guide_Drawings/Steckler_Compartment/Wall_back.ipt"], "Wall_back.ipt"),
        ([f"{RAW}/firemodels/cad/master/FDS_Validation_Guide_Drawings/Steckler_Compartment/Wall_door.ipt"], "Wall_door.ipt"),
    ],
    "rhino_3dm":  [([f"https://files.mcneel.com/TEST/Rhino Logo.3dm", f"{RAW}/mcneel/opennurbs/9.x/example_files/V2/v2_my_mesh.3dm"], "Rhino_Logo.3dm")],
    "autocad_dwg": [([f"{NIST}/models/Autodesk/blowdryer/MOTOR_SHELL.dwg"], "MOTOR_SHELL.dwg")],
    "autocad_dxf": [([f"{ASSIMP}/DXF/PinkEggFromLW.dxf"], "PinkEggFromLW.dxf")],
    "revit_rfa":  [([f"{RAW}/UserDevtec/Revit-RFA-File-Extractor/main/racbasicsamplefamily.rfa",
                     f"{RAW}/UserDevtec/Revit-RFA-File-Extractor/master/racbasicsamplefamily.rfa",
                     f"{RAW}/rabiadhikari/Revit-Families/master/Parking Tag.rfa"], "family.rfa")],
    "revit_rvt":  [([f"{RAW}/rabiadhikari/Revit-Families/master/Parking Example Share.rvt"], "Parking_Example_Share.rvt")],
    "ifc":        [([f"{ASSIMP}/IFC/AC14-FZK-Haus.ifc"], "AC14-FZK-Haus.ifc")],
    "microstation_dgn": [([f"{NIST}/models/Bentley/shaft.dgn"], "shaft.dgn")],
    "stl":        [([f"{ASSIMP}/STL/Spider_ascii.stl", f"{ASSIMP}/STL/Wuson.stl"], "Spider_ascii.stl")],
    "obj":        [([f"{ASSIMP}/OBJ/spider.obj"], "spider.obj")],
    "fbx":        [([f"{ASSIMP}/FBX/spider.fbx", f"{ASSIMP}/FBX/box.fbx"], "spider.fbx")],
    "collada_dae": [([f"{ASSIMP}/Collada/duck.dae"], "duck.dae")],
    "3ds":        [([f"{ASSIMP}/3DS/fels.3ds", f"{ASSIMP}/3DS/test1.3ds", f"{ASSIMP}/3DS/cube_with_specular_texture.3DS"], "model.3ds")],
    "3mf":        [([f"{ASSIMP}/3MF/box.3mf"], "box.3mf")],
    "gltf_glb":   [([f"{RAW}/KhronosGroup/glTF-Sample-Assets/main/Models/Duck/glTF-Binary/Duck.glb",
                     f"{RAW}/KhronosGroup/glTF-Sample-Models/master/2.0/Duck/glTF-Binary/Duck.glb"], "Duck.glb")],
}


def fetch(url: str) -> bytes:
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(parts.path)))
    req = urllib.request.Request(url, headers={"User-Agent": "cadview-sample-fetch"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "samples" / "web"
    report = {}
    for label, entries in SOURCES.items():
        d = root / label
        d.mkdir(parents=True, exist_ok=True)
        for urls, name in entries:
            dest = d / name
            if dest.exists() and dest.stat().st_size > 0:
                report[f"{label}/{name}"] = {"status": "cached", "bytes": dest.stat().st_size}
                continue
            last = None
            for url in urls:
                try:
                    data = fetch(url)
                    if len(data) < 64 or data[:15].lower().startswith(b"version https:"):  # empty / Git LFS pointer
                        last = f"too small or LFS pointer ({len(data)} B)"
                        continue
                    dest.write_bytes(data)
                    report[f"{label}/{name}"] = {"status": "ok", "bytes": len(data), "url": url}
                    break
                except Exception as exc:  # noqa: BLE001
                    last = str(exc)
            else:
                report[f"{label}/{name}"] = {"status": "missing", "error": last, "tried": urls}
    (root / "sources.json").write_text(json.dumps(report, indent=2))
    for k, v in report.items():
        print(f"{v['status']:8} {k:55} {v.get('bytes', '')} {v.get('error', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
