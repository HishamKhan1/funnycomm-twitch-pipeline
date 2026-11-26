@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 goto badfolder

echo FunnyComm Twitch-to-YouTube Pipeline - installer
echo.
if exist ".pipeline_venv\Scripts\python.exe" goto dependencies

where py >nul 2>nul
if errorlevel 1 goto nolauncher
py -3.12 --version >nul 2>nul
if errorlevel 1 goto nopython312

echo Creating an isolated Python 3.12 environment...
py -3.12 -m venv ".pipeline_venv"
if errorlevel 1 goto venvfailed

:dependencies
echo Installing required Python packages...
".pipeline_venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto pipfailed
".pipeline_venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto pipfailed

echo.
echo SUCCESS - Base dependencies installed.
echo Next: run Install_Smart_Editing.bat once, then start with Launch_Pipeline.bat.
pause
exit /b 0

:nolauncher
echo ERROR - Python launcher py was not found.
echo Install Python 3.12 64-bit with the launcher enabled.
goto fail

:nopython312
echo ERROR - Python 3.12 is not registered with the py launcher.
echo Run py -0p to see installed versions.
echo Install Python 3.12 64-bit or activate a compatible environment.
goto fail

:badfolder
echo ERROR - Could not access the installation folder.
goto fail

:venvfailed
echo ERROR - Could not create the isolated Python environment.
goto fail

:pipfailed
echo ERROR - Package installation failed. Review the pip output above.
goto fail

:fail
pause
exit /b 1
