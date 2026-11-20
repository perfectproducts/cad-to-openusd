"""Generate a small coloured STEP assembly for testing the importer.

    python tools/make_sample_step.py samples/demo_assembly.step

The assembly contains exact (non-tessellated) geometry: a filleted base plate with
holes, two instances of a chamfered pin, a torus ring and a revolved knob. Units: mm.
"""

from __future__ import annotations

import sys
from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

_to_edge = getattr(TopoDS, "Edge_s", None) or TopoDS.Edge  # OCP 7.x vs 8.x spelling
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool


def _edges(shape):
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        yield _to_edge(exp.Current())
        exp.Next()


def base_plate():
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-60, -40, 0), 120, 80, 10).Shape()
    fillet = BRepFilletAPI_MakeFillet(plate)
    for e in _edges(plate):
        fillet.Add(2.0, e)
    plate = fillet.Shape()
    for x, y in ((-40, -20), (40, -20), (-40, 20), (40, 20)):
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), 4.0, 12).Shape()
        plate = BRepAlgoAPI_Cut(plate, hole).Shape()
    return plate


def pin():
    shaft = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 6.0, 40).Shape()
    chamfer = BRepFilletAPI_MakeChamfer(shaft)
    for e in _edges(shaft):
        chamfer.Add(1.0, e)
    return chamfer.Shape()


def ring():
    return BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 14.0, 4.0).Shape()


def knob():
    sphere = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 18), 12.0).Shape()
    stem = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 7.0, 14).Shape()
    return BRepAlgoAPI_Fuse(sphere, stem).Shape()


def located(x, y, z):
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(t)


def main(out_path: str) -> None:
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    def set_name(label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))

    def rgb(r, g, b):
        return Quantity_Color(r, g, b, Quantity_TOC_RGB)

    assembly = shapes.NewShape()
    set_name(assembly, "DemoAssembly")

    parts = {
        "BasePlate": (base_plate(), rgb(0.55, 0.58, 0.62)),
        "Pin": (pin(), rgb(0.85, 0.65, 0.15)),
        "Ring": (ring(), rgb(0.12, 0.45, 0.85)),
        "Knob": (knob(), rgb(0.80, 0.15, 0.15)),
    }
    labels = {}
    for name, (shape, color) in parts.items():
        label = shapes.AddShape(shape, False)
        set_name(label, name)
        colors.SetColor(label, color, XCAFDoc_ColorSurf)
        labels[name] = label

    placements = [
        ("BasePlate", (0, 0, 0)),
        ("Pin", (-40, 0, 10)),
        ("Pin", (40, 0, 10)),  # second instance of the same part
        ("Ring", (0, 0, 10 + 4)),
        ("Knob", (0, 0, 10)),
    ]
    for i, (name, (x, y, z)) in enumerate(placements):
        comp = shapes.AddComponent(assembly, labels[name], located(x, y, z))
        set_name(comp, f"{name}_{i}")
    shapes.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.SetColorMode(True)
    writer.SetNameMode(True)
    if not writer.Transfer(doc, STEPControl_AsIs):
        raise SystemExit("STEP transfer failed")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if writer.Write(out_path) != IFSelect_RetDone:
        raise SystemExit(f"Failed to write {out_path}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/demo_assembly.step")
