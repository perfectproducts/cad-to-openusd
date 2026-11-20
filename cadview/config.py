"""Paths and tool locations. Override with environment variables when needed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# usd-brep source build (third_party/build_occt_to_usd.bat) provides occt_to_usd.exe.
USD_BREP_BUILD = Path(os.environ.get("CADVIEW_USD_BREP_BUILD", ROOT / "third_party" / "usd-brep" / "_build"))
OCCT_TO_USD = Path(
    os.environ.get("CADVIEW_OCCT_TO_USD", USD_BREP_BUILD / "windows-x86_64" / "release" / "occt_to_usd.exe")
)
OMNISOLID_PLUGIN_PATH = Path(
    os.environ.get("OMNISOLID_PLUGIN_PATH", USD_BREP_BUILD / "schema" / "omniSolid" / "resources")
)

OUTPUT_DIR = Path(os.environ.get("CADVIEW_OUTPUT", ROOT / "output"))
PYTHON = sys.executable  # workers run in the same venv, but in separate processes

# Exact-BRep route (OpenCASCADE -> occt_to_usd -> usd-brep).
BREP_EXTENSIONS = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}
SMLIB_EXTENSIONS = {".smb"}
USD_EXTENSIONS = {".usd", ".usda", ".usdc", ".usdz"}
# Everything else goes through NVIDIA's usd-convert-cad (HOOPS, tessellated output).
HOOPS_EXTENSIONS = {
    ".jt", ".dgn", ".catpart", ".catproduct", ".cgr", ".3dxml", ".ifc", ".ifczip", ".prt", ".asm",
    ".par", ".pwd", ".psm", ".xmt", ".x_t", ".x_b", ".xmt_txt", ".sldprt", ".sldasm", ".ipt", ".iam",
    ".dwg", ".dxf", ".rvt", ".rfa", ".3dm", ".stl", ".dae", ".fbx", ".obj", ".3ds", ".3mf", ".gltf",
    ".glb", ".sat", ".sab",
}  # fmt: skip
SUPPORTED_EXTENSIONS = BREP_EXTENSIONS | SMLIB_EXTENSIONS | USD_EXTENSIONS | HOOPS_EXTENSIONS

# Tessellation presets: angle tolerance (deg) and chord height relative to part size.
QUALITY = {
    "coarse": {"angle": 30.0, "chord_rel": 0.004, "hoops_lod": 1},
    "medium": {"angle": 15.0, "chord_rel": 0.001, "hoops_lod": 2},
    "fine": {"angle": 6.0, "chord_rel": 0.0003, "hoops_lod": 4},
}


def occt_to_usd_env() -> dict[str, str]:
    """Environment for the natively built occt_to_usd.exe (its OpenUSD DLLs + schema)."""
    env = dict(os.environ)
    usd = USD_BREP_BUILD / "target-deps" / "usd" / "release"
    dirs = [OCCT_TO_USD.parent, usd / "lib", usd / "bin"]
    env["PATH"] = os.pathsep.join([*(str(d) for d in dirs), env.get("PATH", "")])
    env["OMNISOLID_PLUGIN_PATH"] = str(OMNISOLID_PLUGIN_PATH)
    env.pop("PXR_PLUGINPATH_NAME", None)
    return env
