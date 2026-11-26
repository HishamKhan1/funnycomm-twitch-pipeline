@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo FunnyComm smart editing: speech, laughter, chat reading and nudity scan (free, offline)
echo.
if exist ".pipeline_venv\Scripts\python.exe" goto install
echo Run Install_Pipeline.bat first.
goto fail

:install
".pipeline_venv\Scripts\python.exe" -m pip install "faster-whisper>=1.1,<2"
if errorlevel 1 goto fail
rem nudenet would pull a second OpenCV build; everything it needs is already installed.
".pipeline_venv\Scripts\python.exe" -m pip install --no-deps "nudenet==3.4.2" "rapidocr-onnxruntime==1.4.4"
if errorlevel 1 goto fail
".pipeline_venv\Scripts\python.exe" -m pip install "sherpa-onnx==2.0.3" pyclipper six "Shapely!=2.0.4,>=1.7.1" PyYAML
if errorlevel 1 goto fail
".pipeline_venv\Scripts\python.exe" -c "from funnycomm import speech; import sys; sys.exit(0 if speech.pick_model()[0]==speech.BEST else 1)"
if not errorlevel 1 goto check
echo Downloading the speech model once (about 1.5 GB)...
".pipeline_venv\Scripts\python.exe" -c "from faster_whisper.utils import download_model; download_model('distil-whisper/distil-large-v3.5-ct2')"
if errorlevel 1 goto fail

:check
".pipeline_venv\Scripts\python.exe" -c "from funnycomm import speech, safety, vision, ears; print('Laugh finder :', ears.tagger_ready()); print('Chat reader  :', ears.ocr_ready()); print('Speech model :', speech.pick_model()[0]); print('Nudity scan  :', safety.nude_ready()); print('Face model   :', vision.face_model_ready())"
if errorlevel 1 goto fail
echo.
echo DONE. Restart FunnyComm.
pause
exit /b 0

:fail
echo.
echo Installation did not finish. Copy the message above when asking for help.
pause
exit /b 1
