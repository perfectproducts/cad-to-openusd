"""CAD -> OpenUSD import pipeline (process orchestration only; no OpenUSD imports here).

Routes
------
exact BRep  (.step .stp .iges .igs .brep):
    OpenCASCADE (XCAF) -> per-part OCCT .brep            [workers.occt_export]
    -> occt_to_usd.exe -> per-part BrepArray USD         [usd-brep, built from source]
    -> validate + (heal) + tessellate                    [workers.brep_tess, usd-brep wheel]
    -> assembly layer with transforms, names, colours    [workers.usd_compose]
SMLib (.smb):   usd-brep reads it, exports BrepArray, then as above.
USD with BrepArray prims: usd-brep tessellates the layer for display.
other CAD (.jt, .x_t, .catpart, .sldprt, ...): usd-convert-cad (HOOPS, tessellated meshes).

CLI:  python -m cadview.pipeline <file> [--quality coarse|medium|fine] [--heal] [--route auto|brep|mesh] [--format usdc|usda]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config

Progress = Callable[[str, float], None]


@dataclass
class ImportOptions:
    quality: str = "medium"
    heal: bool = False
    route: str = "auto"  # auto | brep | mesh
    usd_format: str = "usdc"  # usdc (binary crate) | usda (text) for every USD file the import writes

    def __post_init__(self) -> None:
        if self.usd_format not in ("usdc", "usda"):
            raise ValueError(f"usd_format must be 'usdc' or 'usda', not {self.usd_format!r}")

    @property
    def ext(self) -> str:
        return "." + self.usd_format


@dataclass
class ImportResult:
    source: str
    usd_path: str
    route: str
    info: dict
    out_dir: str
    seconds: float
    parts: int = 0
    exact_parts: int = 0
    fallback_parts: int = 0
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


class ImportFailed(RuntimeError):
    def __init__(self, message: str, log: list[str]):
        super().__init__(message)
        self.log = log


def _no_progress(msg: str, frac: float) -> None:
    pass


class _Run:
    def __init__(self, progress: Progress):
        self.progress = progress
        self.log: list[str] = []

    def note(self, msg: str, frac: float | None = None) -> None:
        self.log.append(msg)
        if frac is not None:
            self.progress(msg, frac)

    def run(self, args: list[str], *, env: dict | None = None, what: str, timeout: float = 3600) -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(config.ROOT), timeout=timeout, creationflags=flags,
        )  # fmt: skip
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            self.log.append(f"{what} failed (exit {proc.returncode}):\n  " + "\n  ".join(tail))
            raise ImportFailed(f"{what} failed", self.log)
        return proc.stdout

    def worker(self, module: str, *args: str, what: str) -> dict:
        out = self.run([config.PYTHON, "-m", f"cadview.workers.{module}", *args], what=what)
        for line in reversed(out.strip().splitlines()):
            if line.startswith("{"):
                data = json.loads(line)
                if "error" in data or "fatal" in data:
                    self.log.append(f"{what}: {data.get('error') or data.get('fatal')}")
                    raise ImportFailed(f"{what} failed", self.log)
                return data
        return {}


def _route_for(path: Path, options: ImportOptions) -> str:
    ext = path.suffix.lower()
    if ext in config.USD_EXTENSIONS:
        return "usd"
    if ext in config.SMLIB_EXTENSIONS:
        return "smlib"
    if ext in config.BREP_EXTENSIONS and options.route != "mesh":
        if ext in {".brep", ".brp"} or config.OCCT_TO_USD.exists():
            return "brep"
    if ext in config.HOOPS_EXTENSIONS or ext in {".step", ".stp", ".iges", ".igs"}:
        return "hoops"
    raise ImportFailed(f"Unsupported file type: {ext}", [])


def import_cad(source: str | os.PathLike, options: ImportOptions | None = None, progress: Progress | None = None) -> ImportResult:
    options = options or ImportOptions()
    r = _Run(progress or _no_progress)
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise ImportFailed(f"File not found: {src}", [])
    t0 = time.perf_counter()
    route = _route_for(src, options)
    out_dir = config.OUTPUT_DIR / f"{src.stem}_{src.suffix.lstrip('.').lower()}"  # part.step vs part.jt
    if out_dir.exists():
        shutil.rmtree(out_dir)  # our own per-file output folder
    out_dir.mkdir(parents=True)
    r.note(f"Importing {src.name} via '{route}' route -> {out_dir} (.{options.usd_format})", 0.02)

    if route == "brep":
        try:
            result = _brep_route(r, src, out_dir, options)
        except ImportFailed as exc:
            if src.suffix.lower() not in {".step", ".stp", ".iges", ".igs"}:
                raise
            r.note(f"WARNING exact-BRep route failed ({exc}); retrying with usd-convert-cad", 0.3)
            shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True)
            result = _hoops_route(r, src, out_dir, options)
            result.warnings.insert(0, f"OpenCASCADE could not read this file for the exact-BRep route ({exc}); converted with usd-convert-cad instead")
    elif route == "smlib":
        result = _smlib_route(r, src, out_dir, options)
    elif route == "usd":
        result = _usd_route(r, src, out_dir, options)
    else:
        result = _hoops_route(r, src, out_dir, options)

    result.seconds = round(time.perf_counter() - t0, 2)
    result.log = r.log
    r.note(f"Done in {result.seconds:.1f}s: {result.usd_path}", 1.0)
    (out_dir / "import_report.json").write_text(json.dumps(result.__dict__, indent=2, default=str))
    return result


def _tess_job(manifest: dict, out_dir: Path, options: ImportOptions, items: list[dict], smb: list[dict] | None = None) -> Path:
    q = config.QUALITY[options.quality]
    job = {
        "items": items,
        "smb": smb or [],
        "heal": options.heal,
        "validate": True,
        "curve_angle_deg": q["angle"],
        "surface_angle_deg": q["angle"],
    }
    path = out_dir / "brep_job.json"
    path.write_text(json.dumps(job, indent=2))
    return path


def _finish_parts(r: _Run, manifest: dict, out_dir: Path, options: ImportOptions, brep_ok: dict[str, bool], smb=None) -> tuple[int, int, list[str]]:
    """Validate/tessellate every BrepArray part with usd-brep; fall back to OCCT meshing."""
    q = config.QUALITY[options.quality]
    parts = manifest["parts"]
    items = []
    for p in parts:
        if brep_ok.get(p["id"]):
            item = {"key": p["id"], "brep_usd": str(out_dir / p["brep_usd"]), "out": str(out_dir / f"parts/{p['id']}{options.ext}")}
            if p.get("size"):
                item["chord_height"] = p["size"] * q["chord_rel"]
            items.append(item)
    r.note(f"usd-brep: validating and tessellating {len(items)} BrepArray part(s)", 0.55)
    res = r.worker("brep_tess", str(_tess_job(manifest, out_dir, options, items, smb)), what="usd-brep tessellation")
    results = res.get("results", {})
    r.note(f"usd-brep {res.get('usd_brep_version', '?')} finished", 0.75)

    warnings: list[str] = []
    exact = fallback = 0
    for p in parts:
        pr = results.get(p["id"], {})
        if pr.get("ok"):
            p["usd"] = f"parts/{p['id']}{options.ext}"
            p["geometry"] = "usd-brep"
            exact += 1
            if pr.get("valid") is False:
                warnings.append(f"{p['name']}: BRep failed geometry validation ({pr.get('validation')})")
            if pr.get("mesh_failures"):
                warnings.append(f"{p['name']}: {len(pr['mesh_failures'])} BRep(s) failed to tessellate")
            continue
        reason = pr.get("error") or ("occt_to_usd failed" if not brep_ok.get(p["id"]) else "no mesh produced")
        if p.get("brep") and (out_dir / p["brep"]).exists():
            try:
                text = out_dir / f"parts/{p['id']}_occt.usda"
                r.worker("occt_export", "mesh", str(out_dir / p["brep"]), str(text), what=f"OCCT mesh fallback for {p['name']}")
                if options.usd_format == "usdc":
                    r.worker("usd_compose", "convert", str(text), str(text.with_suffix(".usdc")), what="usda -> usdc")
                    text.unlink()
                p["usd"] = f"parts/{p['id']}_occt{options.ext}"
                p["geometry"] = "occt-fallback"
                fallback += 1
                warnings.append(f"{p['name']}: usd-brep path failed ({reason}); used OpenCASCADE mesh fallback")
                continue
            except ImportFailed:
                pass
        p["geometry"] = "missing"
        warnings.append(f"{p['name']}: no geometry ({reason})")
    for w in warnings:
        r.note("WARNING " + w)
    return exact, fallback, warnings


def _occt_to_usd(r: _Run, out_dir: Path, parts: list[dict], ext: str) -> dict[str, bool]:
    env = config.occt_to_usd_env()
    exe = str(config.OCCT_TO_USD)

    def one(p: dict) -> tuple[str, bool, str]:
        p["brep_usd"] = f"parts/{p['id']}_brep{ext}"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            [exe, str(out_dir / p["brep"]), str(out_dir / p["brep_usd"])],
            capture_output=True, text=True, errors="replace", env=env, timeout=600, creationflags=flags,
        )  # fmt: skip
        return p["id"], proc.returncode == 0, (proc.stderr or proc.stdout).strip()[-400:]

    ok: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) // 2)) as pool:
        for pid, success, msg in pool.map(one, parts):
            ok[pid] = success
            if not success:
                r.note(f"occt_to_usd failed for {pid}: {msg}")
    return ok


def _brep_route(r: _Run, src: Path, out_dir: Path, options: ImportOptions) -> ImportResult:
    if not config.OCCT_TO_USD.exists():
        raise ImportFailed(f"occt_to_usd not found at {config.OCCT_TO_USD}; run third_party\\build_occt_to_usd.bat", r.log)
    r.note("OpenCASCADE: reading CAD file and splitting it into exact BRep parts", 0.08)
    r.worker("occt_export", "export", str(src), str(out_dir), what="OpenCASCADE import")
    manifest_path = out_dir / "occt_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    parts = manifest["parts"]
    r.note(f"OpenCASCADE: {len(parts)} unique part(s), {sum(p.get('faces', 0) for p in parts)} faces", 0.25)

    r.note("occt_to_usd: writing BrepArray prims (exact NURBS/analytic geometry)", 0.3)
    ok = _occt_to_usd(r, out_dir, parts, options.ext)
    r.note(f"occt_to_usd: {sum(ok.values())}/{len(parts)} part(s) converted", 0.5)

    exact, fallback, warnings = _finish_parts(r, manifest, out_dir, options, ok)
    manifest["pipeline"] = "OpenCASCADE -> occt_to_usd (usd-brep) -> usd-brep tessellation"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    r.note("OpenUSD: composing assembly layer", 0.85)
    usd_path = out_dir / f"{src.stem}{options.ext}"
    info = r.worker("usd_compose", "assemble", str(manifest_path), str(usd_path), what="assembly composition")
    return ImportResult(str(src), str(usd_path), "brep", info, str(out_dir), 0.0, len(parts), exact, fallback, warnings)


def _smlib_route(r: _Run, src: Path, out_dir: Path, options: ImportOptions) -> ImportResult:
    (out_dir / "parts").mkdir()
    part = {"id": "p000", "name": src.stem, "color": None, "brep_usd": f"parts/p000_brep{options.ext}"}
    manifest = {
        "source": str(src), "units": "mm (assumed; .smb is unitless)", "meters_per_unit": 0.001, "up_axis": "Z",
        "parts": [part], "roots": [{"name": src.stem, "matrix": None, "color": None, "part": "p000"}],
        "pipeline": "SMLib .smb -> usd-brep BrepArray -> usd-brep tessellation",
    }  # fmt: skip
    smb = [{"key": "p000", "smb": str(src), "brep_usd": str(out_dir / part["brep_usd"])}]
    exact, fallback, warnings = _finish_parts(r, manifest, out_dir, options, {"p000": True}, smb)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    usd_path = out_dir / f"{src.stem}{options.ext}"
    info = r.worker("usd_compose", "assemble", str(manifest_path), str(usd_path), what="assembly composition")
    return ImportResult(str(src), str(usd_path), "smlib", info, str(out_dir), 0.0, 1, exact, fallback, warnings)


def _usd_route(r: _Run, src: Path, out_dir: Path, options: ImportOptions) -> ImportResult:
    info = r.worker("usd_compose", "info", str(src), what="USD inspection")
    warnings: list[str] = []
    if info.get("brep_arrays", 0) > 0:
        r.note(f"usd-brep: tessellating {info['brep_arrays']} BrepArray prim(s)", 0.3)
        out = out_dir / f"{src.stem}_tessellated{options.ext}"
        q = config.QUALITY[options.quality]
        items = [{"key": "file", "brep_usd": str(src), "out": str(out)}]
        job = _tess_job({}, out_dir, options, items)
        res = r.worker("brep_tess", str(job), what="usd-brep tessellation").get("results", {}).get("file", {})
        if not res.get("ok"):
            raise ImportFailed(f"usd-brep could not tessellate {src.name}: {res.get('error') or res.get('mesh_failures')}", r.log)
        if res.get("valid") is False:
            warnings.append(f"BRep geometry validation failed: {res.get('validation')}")
        info = r.worker("usd_compose", "info", str(out), what="USD inspection")
        return ImportResult(str(src), str(out), "usd+brep", info, str(out_dir), 0.0, info["brep_arrays"], info["brep_arrays"], 0, warnings)
    return ImportResult(str(src), str(src), "usd", info, str(out_dir), 0.0)


def _hoops_route(r: _Run, src: Path, out_dir: Path, options: ImportOptions) -> ImportResult:
    exe = Path(config.PYTHON).with_name("usd-convert-cad.exe")
    cmd = [str(exe)] if exe.exists() else [config.PYTHON, "-m", "usd_convert_cad"]
    usd_path = out_dir / f"{src.stem}{options.ext}"
    lod = config.QUALITY[options.quality]["hoops_lod"]
    warnings = ["Tessellated import (usd-convert-cad): exact BRep is not preserved for this format"]
    # Attempts: default; then include hidden CAD elements; then also convert curves/wireframe
    # (2D drawings and old wireframe models have no surfaces at all).
    attempts = [
        ([], None),
        (["--filter-style", "none", "--load-hidden"], "the file's visible content was empty; hidden CAD elements were included"),
        (["--filter-style", "none", "--load-hidden", "--convert-curves"], "no surfaces found; curves/wireframe were converted as BasisCurves"),
    ]
    info: dict = {}
    for i, (extra, note) in enumerate(attempts):
        r.note(f"usd-convert-cad: converting {src.name} (HOOPS, tessellation LOD {lod}) {' '.join(extra)}", 0.1 + 0.2 * i)
        out = r.run([*cmd, "-i", str(src), "-o", str(usd_path), "--tess-lod", str(lod), *extra], what="usd-convert-cad", timeout=7200)
        r.note(out.strip().splitlines()[-1] if out.strip() else "usd-convert-cad finished", 0.3 + 0.2 * i)
        if not usd_path.exists():
            raise ImportFailed("usd-convert-cad reported success but wrote no file", r.log)
        info = r.worker("usd_compose", "info", str(usd_path), what="USD inspection")
        if info.get("curves"):
            cw = r.worker("usd_compose", "curve_widths", str(usd_path), what="curve widths")
            r.note(f"Set line width {cw.get('width', 0):.4g} on {cw.get('curves', 0)} curve prim(s)")
        if info.get("bounds"):
            if note:
                warnings.append(note)
            break
    else:
        warnings.append("usd-convert-cad produced no visible geometry for this file")
    return ImportResult(str(src), str(usd_path), "hoops", info, str(out_dir), 0.0, warnings=warnings)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cadview.pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--quality", choices=list(config.QUALITY), default="medium")
    ap.add_argument("--heal", action="store_true", help="heal BReps with usd-brep before tessellating")
    ap.add_argument("--route", choices=["auto", "brep", "mesh"], default="auto")
    ap.add_argument("--format", choices=["usdc", "usda"], default="usdc", help="USD file format of the output (default usdc)")
    a = ap.parse_args(argv)
    try:
        res = import_cad(a.file, ImportOptions(a.quality, a.heal, a.route, a.format), lambda m, f: print(f"[{f:4.0%}] {m}", flush=True))
    except ImportFailed as exc:
        print("\n".join(exc.log), file=sys.stderr)
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in res.__dict__.items() if k != "log"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
