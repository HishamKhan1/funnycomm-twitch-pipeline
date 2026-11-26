@echo off
cd /d "%~dp0"
if not exist ".pipeline_venv\Scripts\python.exe" (
 echo Install_Pipeline.bat first.
 pause
 exit /b 1
)
".pipeline_venv\Scripts\python.exe" -m pip install yt-dlp
pause
