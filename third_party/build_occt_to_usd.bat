@echo off
rem Builds usd-brep's occt_to_usd converter (release) with the installed v145 toolset.
rem Run prebuild once first (generates _compiler\vs2022 projects + schema resources).
set "UB=%~dp0usd-brep"
cd /d "%UB%"
if not exist "%UB%\_compiler\vs2022\usd-brep.sln" (
  call "%UB%\prebuild.bat" > "%~dp0prebuild.log" 2>&1
  if errorlevel 1 exit /b 1
)
"%UB%\_build\host-deps\msvc\MSBuild\Current\Bin\MSBuild.exe" "%UB%\_compiler\vs2022\usd-brep.sln" /p:Configuration=release;Platform=x64;PlatformToolset=v145 /verbosity:minimal /m /t:occt_to_usd > "%~dp0build.log" 2>&1
exit /b %errorlevel%
