@echo off
cd /d "%~dp0"
if exist ".pipeline_venv\Scripts\python.exe" (
  ".pipeline_venv\Scripts\python.exe" "%~dp0Pipeline_Headless.py"
) else (
  echo Pipeline not installed.
)
