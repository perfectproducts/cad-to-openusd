@echo off
rem One-time setup: Python 3.12 venv + packages, usd-brep source checkout, occt_to_usd build.
rem Requires: uv, git, Visual Studio Build Tools (MSVC v14x C++ toolset + Windows SDK).
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
  uv venv --python 3.12 .venv || exit /b 1
)
uv pip install --python .venv\Scripts\python.exe -e ".[qa]" || exit /b 1

if not exist "third_party\usd-brep\repo.bat" (
  git clone --depth 1 https://github.com/NVIDIA-Omniverse/usd-brep third_party\usd-brep || exit /b 1
)

rem usd-brep pins msbuild.vs_version = "vs2019" (MSVC v142). Let repo_build discover the
rem installed Visual Studio instead; the build script then compiles with the installed toolset.
powershell -NoProfile -Command "$f='third_party\usd-brep\repo.toml'; (Get-Content $f) -replace '^msbuild.vs_version = \"vs2019\".*', 'msbuild.vs_version = \"\"  # local: vswhere discovery' | Set-Content -Encoding utf8 $f"

if not exist "third_party\usd-brep\_build\windows-x86_64\release\occt_to_usd.exe" (
  call "%ROOT%third_party\build_occt_to_usd.bat" || (echo occt_to_usd build failed - see third_party\build.log & exit /b 1)
)

if not exist "samples\demo_assembly.step" (
  .venv\Scripts\python.exe tools\make_sample_step.py samples\demo_assembly.step
)
echo.
echo Setup complete. Start the viewer with run.bat
