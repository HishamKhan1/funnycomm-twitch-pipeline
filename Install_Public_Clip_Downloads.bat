@echo off
setlocal
cd /d "%~dp0"
if not exist ".pipeline_venv\Scripts\python.exe" (
  echo [ERROR] Pipeline virtual environment missing. Run Install_Pipeline.bat first.
  pause
  exit /b 1
)
echo Installing/upgrading yt-dlp for public clips that you have permission to download...
".pipeline_venv\Scripts\python.exe" -m pip install --upgrade yt-dlp
if errorlevel 1 (
  echo [ERROR] yt-dlp installation failed. See error above.
  pause
  exit /b 1
)
echo Installation complete. Restart FunnyComm Pipeline.
pause
