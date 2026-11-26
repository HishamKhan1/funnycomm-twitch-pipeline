@echo off
cd /d "%~dp0"
echo.
echo This registers a daily 10 AM Windows Task Scheduler job as your current user.
echo The task will discover, download authorized Twitch clips, and render local Shorts.
echo It uploads ONLY Shorts you approved, using the posting choice in Settings.
echo Approved Shorts go public at their scheduled time only if you chose that.
echo.
choice /C YN /M "Register daily automation?"
if errorlevel 2 exit /b 0
schtasks /Create /SC DAILY /ST 10:00 /TN "FunnyComm_Twitch_Pipeline" /TR "\"%~dp0Run_Daily.bat\"" /F
if errorlevel 1 echo Could not register scheduled task. See Windows Task Scheduler for details.
pause
