@echo off
cd /d "%~dp0"
if not exist ".pipeline_venv\Scripts\python.exe" (
 echo Installation required. Running installer...
 call "%~dp0Install_Pipeline.bat"
 if errorlevel 1 exit /b 1
)
start "FunnyComm Pipeline" "%~dp0.pipeline_venv\Scripts\pythonw.exe" "%~dp0Pipeline_App.py"
