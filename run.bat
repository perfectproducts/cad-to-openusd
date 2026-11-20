@echo off
rem Launch the CAD -> OpenUSD viewer. Optional argument: a file to import on start.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m cadview %*
