# How this tool was built

This project was built in one Claude Code session. Below are the prompts in the order they were
given, each followed by what was done and what was learned. Paths are relative to the repository
root.

---

## 1. The initial request

> create a little local tool application with a 3d view using ovrtx and ovui which allows to import
> and view a CAD file like STEP or nontriangulated JT or some other supported format to OpenUSD using
> https://github.com/NVIDIA-Omniverse/usd-brep and the omniverse libraries

Added while the work was under way:

> the realtime viewer skill may also help :
> https://github.com/NVIDIA/skills/blob/main/skills/omniverse-realtime-viewer/SKILL.md

**Research**

- `usd-brep` (PyPI 1.0.2): CPython 3.12 only. It bundles OpenUSD 25.11. It reads and writes
  `BrepArray` prims, validates, heals and tessellates them, and reads SMLib `.smb` files. It has
  **no STEP or JT reader**. Its CAD bridge is the C++ `occt_to_usd` tool (OpenCASCADE `.brep` →
  `BrepArray`), which is not in the wheel.
- `usd-convert-cad` (PyPI 0.4.0): NVIDIA's HOOPS-based converter for STEP, JT, Parasolid, CATIA,
  SolidWorks, Revit, IFC and more. It writes **tessellated `UsdGeomMesh` only** and bundles its own
  `pxr` 25.11.
- `ovrtx` 0.7 + `ovstage` 0.4 (renderer attached to an application-owned stage) and `ovui` 0.3.1
  (native window). The *omniverse-realtime-viewer* skill's `ovui-local-viewer-recipe` defined the
  architecture: one desktop process, one owner for renderer and stage, `LdrColor` shown through a
  `ByteImageProvider`, and the user's USD never modified.

**Decision asked of the user:** how CAD should reach usd-brep as exact BRep. Options were a Python
translator, building usd-brep's C++ tools, or meshes only. **Answer: build usd-brep from source.**

**Steps**

1. Created a Python 3.12 venv with `ovrtx`, `ovstage`, `ovui`, `usd-brep`, `usd-convert-cad`,
   `cadquery-ocp-novtk` (OpenCASCADE) and `numpy`.
2. Cloned usd-brep into `third_party/usd-brep`. The repo pins MSVC v142 and only VS 18 Build Tools
   (v145) were installed. Switching `msbuild.vs_version` to vswhere discovery and calling MSBuild
   with `PlatformToolset=v145` built `occt_to_usd.exe` without installing anything else
   (`third_party/build_occt_to_usd.bat`).
3. Proved the chain on usd-brep's test `.brep` files: `occt_to_usd` → `BrepArray` → the PyPI
   `usd_brep` imports, validates and `tessellate_file`s it.
4. Wrote the import workers. Each runs in its own process, because three OpenUSD builds must never
   share one:
   - `cadview/workers/occt_export.py`: OpenCASCADE XCAF reads STEP/IGES/BREP and writes one `.brep`
     per unique part plus a manifest (assembly tree, transforms, names, colours).
   - `cadview/workers/brep_tess.py`: usd-brep validates, optionally heals and tessellates.
   - `cadview/workers/usd_compose.py`: usd-convert-cad's `pxr` writes the assembly layer and reports
     bounds and metrics.
   - `cadview/pipeline.py`: orchestration and CLI, with a per-part OpenCASCADE mesh fallback.
5. Generated a coloured STEP test assembly with fillets, holes and an instanced part
   (`tools/make_sample_step.py`).
6. Viewer: `cadview/renderer_runtime.py` is a render thread that owns the `ovrtx.Renderer` and the
   `ovstage.Stage`. It writes the camera's `omni:xform` at ordinal N, advances the write floor, then
   steps at N. `cadview/app.py` holds the ovui window, toolbar, letterboxed viewport input, info
   panel and log. `cadview/camera.py` is the orbit camera.
7. Validated with ovui's inspector (`CADVIEW_INSPECT=1`): framebuffer screenshots and real mouse and
   keyboard input for typing a path, importing, orbit, pan, zoom and Fit.

**Findings**

- A GDI desktop capture of the Vulkan window comes back black. Use the ovui inspector instead.
- The first `attach_ovstage` in a new ovui + ovrtx process compiles RTX shaders, which took about
  2 minutes. This looked like a deadlock. Later starts take about 3 s, and the status bar now
  explains the wait.
- A real-world STEP assembly (OpenCASCADE's `as1-oc-214`, with nested sub-assemblies and 6
  instanced bolts) imports as exact BRep in about 1.5 s.

## 2. Supported formats

> What cad formats are supported?

Answered from `cadview/config.py`. The exact-BRep route covers STEP, IGES, OpenCASCADE `.brep`,
SMLib `.smb` and USD with `BrepArray` prims. The mesh route (usd-convert-cad) covers JT, Parasolid,
ACIS, CATIA, SolidWorks, NX/Creo/Solid Edge, Inventor, Rhino, DWG/DXF, Revit, IFC, DGN, STL, OBJ,
FBX, DAE, 3DS, 3MF and glTF.

## 3. Testing with real files

> Can you find sample files for each format on the internet and try for each ?

1. Found public samples on GitHub: the NIST engineering design models mirror, assimp test models,
   firemodels/cad, CATIA and SolidWorks portfolios, Revit family repos, Khronos glTF and McNeel
   openNURBS. `tools/fetch_web_samples.py` downloads them to `samples/web/` and records the URLs.
2. `tools/format_matrix.py` imports and renders every sample and writes a report and a contact
   sheet.
3. **Pipeline bugs the samples exposed, all fixed:**
   - Output folders were keyed by file stem, so `spider.obj` and `spider.fbx` overwrote each
     other. They are now `<stem>_<ext>`.
   - A 1995 STEP file without product structure could not be read by OpenCASCADE. Unreadable
     STEP/IGES now falls back to usd-convert-cad.
   - The Revit project converted with every mesh hidden. The importer now retries with
     `--filter-style none --load-hidden`.
   - The DWG and DGN samples were wireframe-only. The importer now retries with `--convert-curves`
     and sets a visible curve width, overriding the converter's `primvars:widths`.
   - The render thread no longer stops when one scene fails to load.
4. **Result: 33 of 37 samples imported and rendered.** The failures were Creo/Pro/E V18 (*file too
   old*), Solid Edge `.par` and `.psm` (no geometry, invalid format) and the only 3D XML sample (no
   geometry). No public `.sab` or `.pwd` samples were found.

## 4. Export format

> add a toggle to switch between usda and usdc export (usdc default)

- Added `ImportOptions.usd_format` (`usdc` by default), applied to every file an import writes: the
  assembly, the `BrepArray` parts, the tessellated parts, HOOPS output and tessellated USD inputs.
  The OpenCASCADE fallback mesh is written as text and converted with `pxr` when USDC is selected.
- The ovui toolbar has a **USDC | USDA** toggle, and the CLIs take `--format usdc|usda`.
- Verified every route in both formats by file header (`PXR-USDC` or `#usda`) and in the UI.

## 5. Report and render service

> can you give me an artifact on the different files and the results ? can you render this with
> the render service at :8000 ?

- The service at `:8000` is an ovrtx Render Service: `POST /render` with a USD path and an
  existing camera prim. `tools/render_service_shots.py` writes a small shot layer per converted
  file, which sublayers it unchanged and adds a framed camera, then submits it and saves the image.
  All 33 rendered in about 0.5 s each.
- The service adds its own lighting, so the viewer lights are now opt-in (`--shot-lights`).
- The service needs absolute paths. For a path it cannot resolve, it reports `done` and returns a
  solid magenta frame.
- Published the results as a private report page in the user's claude.ai account. A self-contained copy, with fonts and images embedded, is
  [docs/format-matrix.html](docs/format-matrix.html).

## 6. Publishing

> create a repository on https://github.com/perfectproducts and commit and push
>
> also add a readme with the prompts and steps from our session

Created `perfectproducts/cad-to-openusd` (public) and added this file.

> add an Apache-2.0 license and push

> also add the artifacts as self contained html

Added `LICENSE` (Apache-2.0) and `docs/format-matrix.html`.

---

### Reproduce

```bat
setup.bat                                        & rem venv, packages, usd-brep build
run.bat samples\demo_assembly.step               & rem viewer
.venv\Scripts\python tools\fetch_web_samples.py  & rem public samples (not committed)
.venv\Scripts\python tools\format_matrix.py      & rem import + render every sample
```
