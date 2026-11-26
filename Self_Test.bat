@echo off
cd /d "%~dp0"
if exist ".pipeline_venv\Scripts\python.exe" goto run
echo Run Install_Pipeline.bat first.
pause
exit /b 1

:run
set PYTHONUTF8=1
echo Running offline tests in a throw-away folder. Your data is not touched...
".pipeline_venv\Scripts\python.exe" -W ignore tests\test_pipeline.py
if errorlevel 1 goto bad
echo.
echo ALL TESTS PASSED
pause
exit /b 0

:bad
echo.
echo SOMETHING FAILED. Copy the text above when asking for help.
pause
exit /b 1
