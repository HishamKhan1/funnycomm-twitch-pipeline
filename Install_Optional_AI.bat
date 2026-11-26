@echo off
cd /d "%~dp0"
if not exist ".pipeline_venv\Scripts\python.exe" (
 echo Run Install_Pipeline.bat first.
 pause
 exit /b 1
)
".pipeline_venv\Scripts\python.exe" -m pip install "faster-whisper>=1,<2" "mediapipe>=0.10.21,<0.11"
if errorlevel 1 goto fail
if not exist "engine\models" mkdir "engine\models"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $u='https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter_landscape/float16/latest/selfie_segmenter_landscape.tflite'; Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile 'engine\models\selfie_segmenter_landscape.tmp'; Move-Item -Force 'engine\models\selfie_segmenter_landscape.tmp' 'engine\models\selfie_segmenter_landscape.tflite'"
if errorlevel 1 goto fail
".pipeline_venv\Scripts\python.exe" -c "import mediapipe as mp; o=mp.tasks.vision.ImageSegmenterOptions(base_options=mp.tasks.BaseOptions(model_asset_path='engine/models/selfie_segmenter_landscape.tflite')); m=mp.tasks.vision.ImageSegmenter.create_from_options(o); m.close(); print('Selfie model loaded; Person AI ready')"
if errorlevel 1 goto fail
echo Optional local AI installed. Whisper speech model downloads automatically on first use.
pause
exit /b 0
:fail
echo Optional Person AI installation failed. Basic editor remains available.
pause
exit /b 1
