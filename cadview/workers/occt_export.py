"""Worker: read STEP / IGES / OCCT-BRep with OpenCASCADE and split it into parts.

Runs in its own process (OpenCASCADE only; no OpenUSD in this interpreter).

    python -m cadview.workers.occt_export export <input> <out_dir>
        -> <out_dir>/parts/pNNN.brep  (one exact BRep per unique part shape)
        -> <out_dir>/occt_manifest.json (assembly tree, transforms, names, colours)

    python -m cadview.workers.occt_export mesh <part.brep> <out.usda> [rel_deflection]
        -> fallback triangle mesh straight from OpenCASCADE (used only when the
           usd-brep path fails for a part).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESCAFControl import IGESCAFControl_Reader
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_Tool

try:  # OCP 7.x
    from OCP.TDF import TDF_LabelSequence
except ImportError:  # OCP 8.x moved collection instantiations
    from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.XCAFDoc import (
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_ShapeTool,
)

_to_face = getattr(TopoDS, "Face_s", None) or TopoDS.Face  # OCP 7.x vs 8.x spelling


def _s(cls, name):
    """OCP exposes static methods as `Name_s` (7.x) or `Name` (8.x)."""
    return getattr(cls, name + "_s", None) or getattr(cls, name)


STEP_EXT = {".step", ".stp"}
IGES_EXT = {".iges", ".igs"}
BREP_EXT = {".brep", ".brp"}


def _label_entry(label: TDF_Label) -> str:
    s = TCollection_AsciiString()
    _s(TDF_Tool, "Entry")(label, s)
    return s.ToCString()


def _label_name(label: TDF_Label) -> str | None:
    attr = TDataStd_Name()
    if label.FindAttribute(_s(TDataStd_Name, "GetID")(), attr):
        text = attr.Get().ToExtString()
        return text or None
    return None


def _box_limits(box: Bnd_Box) -> tuple[float, float, float, float, float, float]:
    lo, hi = box.CornerMin(), box.CornerMax()
    return lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()


def _trsf_to_usd(loc: TopLoc_Location) -> list[float] | None:
    """gp_Trsf (column-vector, 3x4) -> USD row-vector 4x4, flattened row-major."""
    if loc.IsIdentity():
        return None
    t = loc.Transformation()
    m = [[0.0] * 4 for _ in range(4)]
    for i in range(3):
        for j in range(3):
            m[j][i] = t.Value(i + 1, j + 1)
        m[3][i] = t.Value(i + 1, 4)
    m[3][3] = 1.0
    return [v for row in m for v in row]


class _XcafScene:
    def __init__(self, doc: TDocStd_Document):
        self.shape_tool = _s(XCAFDoc_DocumentTool, "ShapeTool")(doc.Main())
        self.color_tool = _s(XCAFDoc_DocumentTool, "ColorTool")(doc.Main())
        self.parts: list[dict] = []
        self._part_by_entry: dict[str, str] = {}
        self._shapes: dict[str, TopoDS_Shape] = {}

    def color(self, label: TDF_Label) -> list[float] | None:
        c = Quantity_Color()
        for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
            if _s(XCAFDoc_ColorTool, "GetColor")(label, kind, c):
                return [c.Red(), c.Green(), c.Blue()]
        return None

    def part_for(self, label: TDF_Label, name: str) -> str:
        entry = _label_entry(label)
        if entry in self._part_by_entry:
            return self._part_by_entry[entry]
        pid = f"p{len(self.parts):03d}"
        shape = _s(XCAFDoc_ShapeTool, "GetShape")(label)
        self._shapes[pid] = shape
        self.parts.append({"id": pid, "name": name, "color": self.color(label), "brep": f"parts/{pid}.brep"})
        self._part_by_entry[entry] = pid
        return pid

    def node(self, label: TDF_Label, name: str | None, loc: TopLoc_Location | None, color) -> dict:
        st = XCAFDoc_ShapeTool
        name = name or _label_name(label) or "Part"
        node = {"name": name, "matrix": _trsf_to_usd(loc) if loc is not None else None, "color": color}
        if _s(st, "IsAssembly")(label):
            comps = TDF_LabelSequence()
            _s(st, "GetComponents")(label, comps, False)
            node["children"] = []
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                ref = TDF_Label()
                if not _s(st, "GetReferredShape")(comp, ref):
                    continue
                node["children"].append(
                    self.node(ref, _label_name(comp) or _label_name(ref), _s(st, "GetLocation")(comp), self.color(comp))
                )
        else:
            node["part"] = self.part_for(label, _label_name(label) or name)
        return node


def _read_xcaf(path: Path) -> TDocStd_Document:
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    reader = STEPCAFControl_Reader() if path.suffix.lower() in STEP_EXT else IGESCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    if path.suffix.lower() in STEP_EXT:
        reader.SetLayerMode(True)
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read {path.name}")
    if not reader.Transfer(doc):
        raise RuntimeError(f"OpenCASCADE could not transfer {path.name}")
    return doc


def export(input_path: str, out_dir: str) -> dict:
    src = Path(input_path)
    out = Path(out_dir)
    (out / "parts").mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()

    if ext in BREP_EXT:
        shape = TopoDS_Shape()
        if not _s(BRepTools, "Read")(shape, str(src), BRep_Builder()):
            raise RuntimeError(f"OpenCASCADE could not read {src.name}")
        parts = [{"id": "p000", "name": src.stem, "color": None, "brep": "parts/p000.brep"}]
        shapes = {"p000": shape}
        roots = [{"name": src.stem, "matrix": None, "color": None, "part": "p000"}]
    elif ext in STEP_EXT | IGES_EXT:
        doc = _read_xcaf(src)
        scene = _XcafScene(doc)
        free = TDF_LabelSequence()
        scene.shape_tool.GetFreeShapes(free)
        roots = [scene.node(free.Value(i), None, None, scene.color(free.Value(i))) for i in range(1, free.Length() + 1)]
        parts, shapes = scene.parts, scene._shapes
    else:
        raise RuntimeError(f"Unsupported extension for the OpenCASCADE path: {ext}")

    if not parts:
        raise RuntimeError(f"No shapes found in {src.name}")

    bbox = Bnd_Box()
    for part in parts:
        shape = shapes[part["id"]]
        if not _s(BRepTools, "Write")(shape, str(out / part["brep"])):
            raise RuntimeError(f"Failed to write {part['brep']}")
        pbox = Bnd_Box()
        _s(BRepBndLib, "Add")(shape, pbox)
        if not pbox.IsVoid():
            xmin, ymin, zmin, xmax, ymax, zmax = _box_limits(pbox)
            part["size"] = max(xmax - xmin, ymax - ymin, zmax - zmin)
            bbox.Add(pbox)
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        n = 0
        while exp.More():
            n += 1
            exp.Next()
        part["faces"] = n

    manifest = {
        "source": str(src.resolve()),
        "units": "mm",  # OpenCASCADE's STEP/IGES readers convert to millimetres by default
        "meters_per_unit": 0.001,
        "up_axis": "Z",
        "parts": parts,
        "roots": roots,
    }
    (out / "occt_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def mesh(brep_path: str, out_usda: str, rel_deflection: float = 0.002) -> None:
    """OpenCASCADE triangulation fallback, written as a plain UsdGeomMesh layer."""
    shape = TopoDS_Shape()
    if not _s(BRepTools, "Read")(shape, brep_path, BRep_Builder()):
        raise RuntimeError(f"cannot read {brep_path}")
    box = Bnd_Box()
    _s(BRepBndLib, "Add")(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = _box_limits(box)
    diag = math.dist((xmin, ymin, zmin), (xmax, ymax, zmax)) or 1.0
    BRepMesh_IncrementalMesh(shape, diag * rel_deflection, False, 0.35, True)

    points: list[tuple[float, float, float]] = []
    indices: list[int] = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = _to_face(exp.Current())
        loc = TopLoc_Location()
        tri = _s(BRep_Tool, "Triangulation")(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            base = len(points)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                points.append((p.X(), p.Y(), p.Z()))
            reverse = face.Orientation() == TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                if reverse:
                    b, c = c, b
                indices += [base + a - 1, base + b - 1, base + c - 1]
        exp.Next()

    pts = ", ".join(f"({x:.9g}, {y:.9g}, {z:.9g})" for x, y, z in points)
    idx = ", ".join(str(i) for i in indices)
    counts = ", ".join("3" for _ in range(len(indices) // 3))
    Path(out_usda).write_text(
        f"""#usda 1.0
(
    defaultPrim = "World"
)

def Xform "World"
{{
    def Mesh "occt_mesh"
    {{
        int[] faceVertexCounts = [{counts}]
        int[] faceVertexIndices = [{idx}]
        point3f[] points = [{pts}]
        uniform token subdivisionScheme = "none"
        custom string cadview:source = "OpenCASCADE BRepMesh fallback"
    }}
}}
"""
    )


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "export":
        m = export(argv[1], argv[2])
        print(json.dumps({"parts": len(m["parts"])}))
        return 0
    if len(argv) >= 3 and argv[0] == "mesh":
        mesh(argv[1], argv[2], float(argv[3]) if len(argv) > 3 else 0.002)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
